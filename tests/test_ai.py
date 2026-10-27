"""Day 3: AI client with sticky Gemini -> Groq fallback (all providers mocked, no network)."""

import json

import httpx
import respx

from app.store import TimelineStore
from tests.support import (DISPUTE_ID, GEMINI, GROQ, DisputeOnPayPal, ai_reply, events,
                           fight, make_client, open_dispute, setup_paypal, shipped_order)


def regenerate(client):
    return client.post(f"/api/disputes/{DISPUTE_ID}/recommendation").json()


def disputed(tmp_path, gemini, groq=None):
    """Client with a shipped, disputed ORDER1. The dispute webhook triggers the first AI call."""
    setup_paypal()
    DisputeOnPayPal()
    # A Response is returned every time; a list is returned in order; an exception is raised.
    gemini_route = respx.post(GEMINI)
    if isinstance(gemini, httpx.Response):
        gemini_route.mock(return_value=gemini)
    else:
        gemini_route.mock(side_effect=gemini)
    groq_route = respx.post(GROQ).mock(return_value=groq or ai_reply(fight()))
    client, settings = make_client(tmp_path)
    shipped_order(client)
    open_dispute(client)
    return client, settings, gemini_route, groq_route


@respx.mock
def test_gemini_ok_stores_recommendation(tmp_path):
    client, _, gemini, groq = disputed(tmp_path, ai_reply(fight()))
    [rec] = events(client, "ORDER1", "ai_recommendation")
    p = rec["payload"]
    assert p["provider"] == "gemini" and p["model"] == "gemini-test-lite"
    assert p["prompt_version"] and isinstance(p["latency_ms"], int)
    assert p["recommendation"]["decision"] == "FIGHT"
    assert p["dispute_update_time"] == "2026-10-26T17:01:43.853Z"
    assert gemini.call_count == 1 and not groq.called

    # Strict json_schema request with Bearer auth; every schema field is required.
    sent = json.loads(gemini.calls[0].request.content)
    assert gemini.calls[0].request.headers["Authorization"] == "Bearer g-test"
    fmt = sent["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


@respx.mock
def test_gemini_429_switches_to_groq_and_persists(tmp_path):
    client, settings, gemini, groq = disputed(tmp_path, httpx.Response(429))
    [rec] = events(client, "ORDER1", "ai_recommendation")
    assert rec["payload"]["provider"] == "groq"
    assert rec["payload"]["model"] == "openai/gpt-oss-120b"
    assert gemini.call_count == 1 and groq.call_count == 1

    # Survives a restart: a fresh store on the same file still says groq.
    assert TimelineStore(settings.database_path).ai_state()["active_provider"] == "groq"
    [switch] = events(client, "other", "ai_provider_switched")
    assert switch["payload"] == {"from": "gemini", "to": "groq", "reason": "HTTP 429"}
    status = client.get("/api/ai/status").json()
    assert status["active_provider"] == "groq" and status["reason"] == "HTTP 429"
    assert "q-test" not in json.dumps(status) and "g-test" not in json.dumps(status)


@respx.mock
def test_after_switch_calls_go_straight_to_groq(tmp_path):
    client, _, gemini, groq = disputed(tmp_path, httpx.Response(503))
    assert gemini.call_count == 1
    assert regenerate(client)["payload"]["provider"] == "groq"
    assert regenerate(client)["payload"]["provider"] == "groq"
    assert gemini.call_count == 1  # never tried again
    assert groq.call_count == 3


@respx.mock
def test_groq_failure_while_active_records_ai_failed(tmp_path):
    client, settings, gemini, groq = disputed(tmp_path, httpx.Response(500))
    groq.mock(return_value=httpx.Response(500))
    result = regenerate(client)
    assert result["kind"] == "ai_failed" and "groq" in result["payload"]["error"]
    assert gemini.call_count == 1  # only the original failure, no fallback back to Gemini
    assert TimelineStore(settings.database_path).ai_state()["active_provider"] == "groq"


@respx.mock
def test_reset_switches_back_to_gemini(tmp_path):
    client, _, gemini, groq = disputed(tmp_path, httpx.Response(429))
    gemini.mock(return_value=ai_reply(fight()))
    assert client.post("/api/ai/reset").json()["active_provider"] == "gemini"
    assert regenerate(client)["payload"]["provider"] == "gemini"
    assert gemini.call_count == 2 and groq.call_count == 1
    switches = [e["payload"]["to"] for e in events(client, "other", "ai_provider_switched")]
    assert switches == ["groq", "gemini"]


@respx.mock
def test_invalid_json_retries_once_then_falls_back(tmp_path):
    client, _, gemini, groq = disputed(tmp_path, [ai_reply("not json"), ai_reply('{"decision": 1}')])
    [rec] = events(client, "ORDER1", "ai_recommendation")
    assert rec["payload"]["provider"] == "groq"
    assert gemini.call_count == 2  # first try + one retry on the same provider
    # The retry tells the model what was wrong.
    retry_messages = json.loads(gemini.calls[1].request.content)["messages"]
    assert "not valid for the schema" in retry_messages[-1]["content"]
    [switch] = events(client, "other", "ai_provider_switched")
    assert switch["payload"]["reason"].startswith("invalid JSON after retry")


@respx.mock
def test_invalid_json_once_then_valid_stays_on_gemini(tmp_path):
    client, _, gemini, groq = disputed(tmp_path, [ai_reply("{oops"), ai_reply(fight())])
    [rec] = events(client, "ORDER1", "ai_recommendation")
    assert rec["payload"]["provider"] == "gemini" and gemini.call_count == 2
    assert not groq.called


@respx.mock
def test_gemini_400_is_a_bug_not_an_outage(tmp_path):
    client, settings, gemini, groq = disputed(
        tmp_path, httpx.Response(400, json={"error": {"message": "Unknown name response_format"}}))
    [failed] = events(client, "ORDER1", "ai_failed")
    assert "HTTP 400" in failed["payload"]["error"]
    assert "Unknown name" in failed["payload"]["error"]
    assert not groq.called
    assert TimelineStore(settings.database_path).ai_state()["active_provider"] == "gemini"
    assert events(client, "other", "ai_provider_switched") == []


@respx.mock
def test_gemini_401_switches_as_config_error(tmp_path, caplog):
    client, _, _, groq = disputed(tmp_path, httpx.Response(401))
    assert events(client, "ORDER1", "ai_recommendation")[0]["payload"]["provider"] == "groq"
    assert "configuration error" in caplog.text


@respx.mock
def test_gemini_timeout_switches(tmp_path):
    client, _, _, groq = disputed(tmp_path, httpx.ReadTimeout("slow"))
    assert events(client, "ORDER1", "ai_recommendation")[0]["payload"]["provider"] == "groq"
    [switch] = events(client, "other", "ai_provider_switched")
    assert switch["payload"]["reason"] == "timeout"
