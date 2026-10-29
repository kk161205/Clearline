"""Day 8: the Insights assistant (AG Studio's agent framework on our own AI client). The AI is
always mocked with respx: no live AI calls."""

import base64
import json
import re
import shutil
from pathlib import Path

import httpx
import pytest
import respx

from app.insights_ai import (ALLOWED_TOOLS, MAX_TURNS, SKIP_SIGNATURE, TOO_MANY_TURNS, TRUNCATED,
                             chat_tools, estimate_tokens, simplify_schema)
from app.store import TimelineStore
from tests.support import GEMINI, GROQ, make_client
from tests.test_dashboard import mixed  # noqa: F401  (fixture)
from tests.test_insights import insights_view

NODE = shutil.which("node")

PII = ("jane.buyer@example.com", "+1 415 555 0100", "https://evil.example/x")

# Studio-style tool schemas (JSON Schema 2020-12 with $defs, $ref, const and nullable unions).
TOOLS = [
    {"name": "view_schema", "description": "The data model.", "parameters": {"type": "object", "properties": {}}},
    {"name": "execute_query", "description": "Run a query.", "parameters": {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "$defs": {"field": {"type": "object", "properties": {"id": {"type": "string"},
                                                              "aggregation": {"type": ["string", "null"]}},
                            "required": ["id"], "additionalProperties": False}},
        "properties": {"fields": {"type": "array", "items": {"$ref": "#/$defs/field"}},
                       "kind": {"const": "flat"},
                       "limit": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]}},
        "required": ["fields", "kind", "ghost"]}},
    {"name": "remove_widget", "description": "Removes a widget.", "parameters": {"type": "object", "properties": {}}},
    {"name": "delete_expression", "description": "Deletes.", "parameters": {"type": "object", "properties": {}}},
]


def question(text="Which orders need me this week?", *, rounds=0, output=None):
    """A Studio turn: the user's question, then `rounds` rounds of tool calls and results."""
    items = [{"kind": "input", "type": "message", "role": "user", "content": [{"type": "text", "text": text}]}]
    for i in range(rounds):
        items.append({"kind": "output", "type": "function_call", "callId": f"c{i}", "name": "execute_query",
                      "arguments": "{}", "status": "completed"})
        items.append({"kind": "input", "type": "function_call_output", "callId": f"c{i}",
                      "output": output or "| …Y3ZF48 | 2026-10-25 | 120.00 | Waiting |"})
    return {"instructions": "Answer the seller.", "items": items, "tools": TOOLS, "tool_choice": "auto"}


def answer(text="", calls=()):
    return httpx.Response(200, json={"choices": [{"message": {"content": text, "tool_calls": [
        {"id": f"call-{i}", "type": "function", "function": {"name": n, "arguments": a}} for i, (n, a) in enumerate(calls)]}}]})


def sent(route, n=-1):
    return json.loads(route.calls[n].request.content)


@respx.mock
def test_a_turn_goes_to_gemini_with_allowed_tools_only_and_comes_back_as_tool_calls(tmp_path):
    gemini = respx.post(GEMINI).mock(return_value=answer(calls=[("view_schema", "{}"), ("remove_widget", "{}")]))
    client, _ = make_client(tmp_path)
    res = client.post("/api/insights/ai", json=question())
    assert res.status_code == 200
    out = res.json()
    assert out["provider"] == "gemini" and [c["name"] for c in out["tool_calls"]] == ["view_schema"]
    assert "only read the report and arrange widgets" in out["text"]  # remove_widget refused
    body = sent(gemini)
    names = [t["function"]["name"] for t in body["tools"]]
    assert names == ["view_schema", "execute_query"] and set(names) <= ALLOWED_TOOLS
    assert body["messages"][0]["role"] == "system" and "cannot move money" in body["messages"][0]["content"]
    assert body["messages"][-1] == {"role": "user", "content": "Which orders need me this week?"}


def test_tool_schemas_are_simplified_for_the_providers():
    s = simplify_schema(TOOLS[1]["parameters"])
    dump = json.dumps(s)
    for gone in ("$ref", "$defs", "$schema", "const", "additionalProperties", '"null"'):
        assert gone not in dump, gone
    assert s["properties"]["kind"]["enum"] == ["flat"]
    assert s["properties"]["fields"]["items"]["properties"]["aggregation"]["type"] == "string"
    assert s["properties"]["limit"] == {"type": "integer", "minimum": 1}
    assert s["required"] == ["fields", "kind"]  # "ghost" isn't a property


@respx.mock
def test_messages_and_tool_results_are_pii_stripped_but_dates_survive(tmp_path):
    gemini = respx.post(GEMINI).mock(return_value=answer("Two orders need you."))
    client, _ = make_client(tmp_path)
    leaky = f"Mail {PII[0]} or call {PII[1]}, see {PII[2]}"
    turn = question(f"Orders for {PII[0]}?", rounds=1, output=f"| …Y3ZF48 | 2026-10-25 | {leaky} | 1761350400000 |")
    assert client.post("/api/insights/ai", json=turn).status_code == 200
    dump = json.dumps(sent(gemini))
    for secret in PII:
        assert secret not in dump, secret
    assert "2026-10-25" in dump and "1761350400000" in dump  # dates are data, not phone numbers
    tool = next(m for m in sent(gemini)["messages"] if m["role"] == "tool")
    assert tool["tool_call_id"] == "c0" and "[email removed]" in tool["content"]
    assistant = next(m for m in sent(gemini)["messages"] if m.get("tool_calls"))
    assert assistant["tool_calls"][0]["function"]["name"] == "execute_query"


@respx.mock
def test_at_most_six_turns_per_question(tmp_path):
    gemini = respx.post(GEMINI).mock(return_value=answer("ok"))
    client, _ = make_client(tmp_path)
    assert client.post("/api/insights/ai", json=question(rounds=MAX_TURNS - 1)).status_code == 200
    assert gemini.call_count == 1
    out = client.post("/api/insights/ai", json=question(rounds=MAX_TURNS)).json()
    assert out["text"] == TOO_MANY_TURNS and out["tool_calls"] == [] and gemini.call_count == 1  # no 7th call


@respx.mock
def test_groq_answers_this_request_only_and_the_sticky_switch_is_untouched(tmp_path):
    gemini = respx.post(GEMINI).mock(return_value=httpx.Response(400, json={"error": {"message": "bad schema"}}))
    groq = respx.post(GROQ).mock(return_value=answer("From Groq."))
    client, settings = make_client(tmp_path)
    store = TimelineStore(settings.database_path)
    before = store.ai_state()
    assert client.post("/api/insights/ai", json=question()).json()["provider"] == "groq"
    gemini.mock(return_value=answer("From Gemini."))
    assert client.post("/api/insights/ai", json=question()).json()["provider"] == "gemini"  # starts on Gemini again
    assert store.ai_state() == before and before["active_provider"] == "gemini"
    assert not any(e["kind"] == "ai_provider_switched" for e in store.timeline("other"))
    assert groq.call_count == 1


@respx.mock
def test_an_insights_failure_leaves_ai_state_unchanged_even_when_on_groq(tmp_path):
    respx.post(GEMINI).mock(return_value=httpx.Response(503))
    respx.post(GROQ).mock(return_value=httpx.Response(503))
    client, settings = make_client(tmp_path)
    store = TimelineStore(settings.database_path)
    store.set_ai_provider("groq", "dispute assistant switched earlier")
    before = store.ai_state()
    res = client.post("/api/insights/ai", json=question())
    assert res.status_code == 502 and "couldn't answer" in res.json()["detail"]
    assert store.ai_state() == before  # neither read-modified nor reset


@respx.mock
def test_insights_ai_needs_login_and_is_rate_limited_per_seller(tmp_path):
    respx.post(GEMINI).mock(return_value=answer("ok"))
    anonymous, _ = make_client(tmp_path, login=False)
    assert anonymous.post("/api/insights/ai", json=question()).status_code == 401
    client, _ = make_client(tmp_path)
    codes = [client.post("/api/insights/ai", json=question()).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


def test_insights_ai_without_any_ai_key(tmp_path):
    client, _ = make_client(tmp_path, gemini_key=None, groq_key=None)
    res = client.post("/api/insights/ai", json=question())
    assert res.status_code == 503 and "AI key" in res.json()["detail"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_browser_adapter_translates_turns_both_ways(mixed):  # noqa: F811
    client, _ = mixed
    data = client.get("/api/dashboard").json()
    reply = {"id": "turn-1", "text": "Two need you.", "model": "m",
             "tool_calls": [{"id": "call-1", "name": "execute_query", "arguments": '{"kind":"flat"}'}]}
    request = {"instructions": "x", "input": [{"kind": "input", "type": "message", "role": "user", "content": []}],
               "tools": [{"name": "view_schema", "description": "d", "parameters": {"type": "object"}, "extra": 1}],
               "toolChoice": "auto", "model": {"id": "ignored"}}
    page = {"id": "p", "widgets": {"a": {"type": "grid", "dataMapping": {"cols": [{"id": "money.ref"}]}},
                                   "b": {"type": "bar-chart-grouped", "dataMapping": {}}}}
    states = [{"pages": [page], "selectedPageId": "p"},  # b was just added: bind to it
              {"pages": [{**page, "widgets": {"a": page["widgets"]["a"]}, "selection": {"type": "widget", "id": "a"}}],
               "selectedPageId": "p"},  # nothing new: bind to the selected widget
              {"pages": [{**page, "widgets": {"a": page["widgets"]["a"]}}], "selectedPageId": "p"}]  # nothing to bind
    panels = [{"filters": {"collapsed": True}}, {"ai": {"collapsed": True}, "edit": {"collapsed": False}}]
    view = insights_view({**data, "assistant": {"reply": reply, "request": request, "states": states,
                                                "panels": panels}})["assistant"]
    # With the assistant open, Compose ("edit"), Data and Filters collapse; closed, nothing changes.
    assert view["panels"] == [{"filters": {"collapsed": True}, "edit": {"collapsed": True},
                               "data": {"collapsed": True}}, None]
    assert [e["type"] for e in view["events"]] == ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END",
                                                   "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END"]
    assert view["events"][3]["toolCallName"] == "execute_query" and view["events"][4]["delta"] == '{"kind":"flat"}'
    out = view["response"]
    assert out["status"] == "completed" and [o["type"] for o in out["output"]] == ["message", "function_call"]
    assert out["output"][1]["callId"] == "call-1" and out["output"][0]["content"][0]["text"] == "Two need you."
    assert view["body"] == {"instructions": "x", "items": request["input"], "tool_choice": "auto",
                            "tools": [{"name": "view_schema", "description": "d", "parameters": {"type": "object"}}]}
    assert view["targets"] == [{"widgetId": "b", "widgetType": "bar-chart-grouped"},
                               {"widgetId": "a", "widgetType": "grid"}, None]


# ---- Gemini thought signatures ----------------------------------------------------------

SIG = "c2lnbmVkLWJ5LWdlbWluaS10aG91Z2h0"  # an opaque value, as Gemini sends it


def gemini_tool_call(call_id="gem-call-1"):
    """A Gemini 3 reply: a tool call carrying its thought signature in an extra field, plus an
    unknown field on the message, both of which must come back unchanged."""
    return httpx.Response(200, json={"choices": [{"message": {
        "role": "assistant", "content": None, "x_unknown": {"keep": True},
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": "view_schema", "arguments": "{}"},
                        "extra_content": {"google": {"thought_signature": SIG}}}]}}]})


def then_result(turn1_reply, output="| money.ref | text |"):
    """The browser's next turn: the question, the model's call (as Studio echoes it), its result."""
    call = turn1_reply["tool_calls"][0]
    turn = question()
    turn["items"] = turn["items"][:1] + [
        {"kind": "output", "type": "function_call", "callId": call["id"], "name": call["name"],
         "arguments": call["arguments"], "status": "completed"},
        {"kind": "input", "type": "function_call_output", "callId": call["id"], "output": output}]
    return turn


@respx.mock
def test_gemini_thought_signature_comes_back_unchanged_and_never_reaches_the_browser(tmp_path):
    gemini = respx.post(GEMINI).mock(side_effect=[gemini_tool_call(), answer("Two orders need you.")])
    client, _ = make_client(tmp_path)
    first = client.post("/api/insights/ai", json=question())
    assert first.status_code == 200 and SIG not in first.text and "extra_content" not in first.text
    assert first.json()["tool_calls"] == [{"id": "gem-call-1", "name": "view_schema", "arguments": "{}"}]
    second = client.post("/api/insights/ai", json=then_result(first.json()))
    assert second.json()["text"] == "Two orders need you."
    replayed = next(m for m in sent(gemini, 1)["messages"] if m.get("tool_calls"))
    assert replayed["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": SIG}}
    assert replayed["x_unknown"] == {"keep": True}  # every field, not just the signature


@respx.mock
def test_signature_cache_miss_sends_googles_skip_value(tmp_path, caplog):
    gemini = respx.post(GEMINI).mock(return_value=answer("ok"))
    client, _ = make_client(tmp_path)  # a fresh app: nothing cached (e.g. after a restart)
    turn = then_result({"tool_calls": [{"id": "lost-call", "name": "view_schema", "arguments": "{}"}]},
                       output="private result text")
    assert client.post("/api/insights/ai", json=turn).status_code == 200
    call = next(m for m in sent(gemini)["messages"] if m.get("tool_calls"))["tool_calls"][0]
    assert call["extra_content"]["google"]["thought_signature"] == SKIP_SIGNATURE
    assert base64.b64decode(SKIP_SIGNATURE) == b"skip_thought_signature_validator"
    assert "no thought signature cached" in caplog.text and "private result" not in caplog.text


@respx.mock
def test_groq_bound_history_has_no_gemini_fields(tmp_path):
    respx.post(GEMINI).mock(side_effect=[gemini_tool_call(), httpx.Response(503)])
    groq = respx.post(GROQ).mock(return_value=answer("From Groq."))
    client, _ = make_client(tmp_path)
    first = client.post("/api/insights/ai", json=question()).json()
    assert client.post("/api/insights/ai", json=then_result(first)).json()["provider"] == "groq"
    body = json.dumps(sent(groq))
    assert "extra_content" not in body and SIG not in body and "x_unknown" not in body
    assert "gem-call-1" in body  # the call itself is still there, in standard form


# ---- Groq: discriminated unions, self-correction, token budget -------------------------

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "studio_turn.json").read_text(encoding="utf-8"))


def test_query_variant_stays_required_and_is_named_in_the_description():
    for provider in ("gemini", "groq"):
        [schema_tool, query_tool] = chat_tools(FIXTURE["tools"], provider)
        fn = query_tool["function"]
        assert "query.variant is required; one of: aggregation, projection." in fn["description"]
        branches = fn["parameters"]["properties"]["query"]["anyOf"]
        assert [b["properties"]["variant"]["enum"] for b in branches] == [["aggregation"], ["projection"]]
        assert all("variant" in b["required"] for b in branches)


@respx.mock
def test_groq_tool_call_validation_failure_is_retried_once_with_the_error(tmp_path):
    respx.post(GEMINI).mock(return_value=httpx.Response(503))
    rejected = httpx.Response(400, json={"error": {
        "message": "Tool call validation failed: parameters for tool execute_query did not match "
                   "schema: /query: missing properties: 'variant'", "code": "tool_use_failed"}})
    groq = respx.post(GROQ).mock(side_effect=[rejected, answer("Fixed it.")])
    client, _ = make_client(tmp_path)
    assert client.post("/api/insights/ai", json=question()).json()["text"] == "Fixed it."
    note = sent(groq, 1)["messages"][-1]
    assert note["role"] == "user" and "missing properties: 'variant'" in note["content"]
    assert groq.call_count == 2


@respx.mock
def test_groq_gets_the_reduced_trimmed_request_under_3000_tokens(tmp_path, caplog):
    respx.post(GEMINI).mock(return_value=httpx.Response(503))
    groq = respx.post(GROQ).mock(return_value=answer("ok"))
    client, _ = make_client(tmp_path)
    turn = then_result({"tool_calls": [{"id": "c1", "name": "view_schema", "arguments": "{}"}]},
                       output=FIXTURE["view_schema_output"])
    older = [{"kind": "input", "type": "message", "role": "user",
              "content": [{"type": "text", "text": f"Old question {i}"}]} for i in range(5)]
    turn["items"] = older + turn["items"]
    turn["tools"] = FIXTURE["tools"] + TOOLS[2:]  # plus tools Groq must not get
    turn["instructions"] = "Studio guidance. " * 400
    caplog.set_level("INFO")
    assert client.post("/api/insights/ai", json=turn).status_code == 200
    body = sent(groq)
    assert [t["function"]["name"] for t in body["tools"]] == ["view_schema", "execute_query"]
    asked = [m["content"] for m in body["messages"] if m["role"] == "user"]
    assert asked == ["Old question 3", "Old question 4", "Which orders need me this week?"]  # last 3 only
    assert estimate_tokens(body) < 3000, estimate_tokens(body)
    assert re.search(r"Groq request, about \d+ tokens", caplog.text)


@respx.mock
def test_long_tool_results_are_capped_and_flagged(tmp_path):
    gemini = respx.post(GEMINI).mock(return_value=answer("ok"))
    client, _ = make_client(tmp_path)
    turn = then_result({"tool_calls": [{"id": "c1", "name": "execute_query", "arguments": "{}"}]},
                       output="| …ABC123 | Settled | 120.00 |\n" * 400)
    client.post("/api/insights/ai", json=turn)
    tool = next(m for m in sent(gemini)["messages"] if m["role"] == "tool")
    assert len(tool["content"]) < 4200 and tool["content"].endswith(TRUNCATED)


@respx.mock
def test_short_rate_limit_is_retried_long_one_is_busy_503(tmp_path):
    respx.post(GEMINI).mock(return_value=httpx.Response(503))
    soon = httpx.Response(429, json={"error": {"message": "Rate limit reached. Please try again in 10ms."}})
    later = httpx.Response(429, headers={"retry-after": "42"}, json={"error": {"message": "tokens per minute"}})
    groq = respx.post(GROQ).mock(side_effect=[soon, answer("ok"), later])
    client, _ = make_client(tmp_path)
    assert client.post("/api/insights/ai", json=question()).status_code == 200 and groq.call_count == 2
    busy = client.post("/api/insights/ai", json=question())
    assert busy.status_code == 503 and busy.json()["detail"] == "The assistant is busy, try again in a minute."
    assert groq.call_count == 3  # no retry for a long wait
