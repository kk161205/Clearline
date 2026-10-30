"""Auto-sync safety net and the sandbox-only require-evidence helper (no network, no sleeping)."""

import asyncio
import json

import httpx
import respx

from app.paypal import PayPalError
from app.scheduler import run_every
from app.store import TimelineStore
from tests.support import (API, DISPUTE_ID, GEMINI, DisputeOnPayPal, ai_reply, events, fight,
                           make_client, open_dispute, setup_paypal, shipped_order)

DISPUTES = f"{API}/v1/customer/disputes"
REQUIRE = f"{DISPUTES}/{DISPUTE_ID}/require-evidence"


# ---- Scheduler ---------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def test_scheduler_calls_job_every_interval():
    slept, calls = [], []

    async def fake_sleep(seconds):
        slept.append(seconds)

    async def job():
        calls.append(len(calls))

    run(run_every(180, job, sleep=fake_sleep, runs=3))
    assert slept == [180, 180, 180] and len(calls) == 3


def test_scheduler_survives_a_failing_run(caplog):
    calls = []

    async def no_sleep(_):
        pass

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise PayPalError(503, {"name": "SERVICE_UNAVAILABLE"})

    run(run_every(1, flaky, sleep=no_sleep, runs=3))
    assert len(calls) == 3  # the error in run 1 didn't stop runs 2 and 3
    assert "Scheduled job failed" in caplog.text


def test_auto_sync_zero_disables_and_task_stops_on_shutdown(tmp_path):
    client, _ = make_client(tmp_path, auto_sync_minutes=0)
    with client:
        assert client.app.state.auto_sync_task is None

    (tmp_path / "on").mkdir()
    client, _ = make_client(tmp_path / "on", auto_sync_minutes=3)
    with client:
        task = client.app.state.auto_sync_task
        assert task is not None and not task.done()  # sleeping before its first run
    assert task.cancelled()  # shutdown stopped it cleanly


@respx.mock
def test_auto_sync_tick_runs_the_dispute_sync(tmp_path):
    setup_paypal()
    paypal = DisputeOnPayPal()
    gemini = respx.post(GEMINI).mock(return_value=ai_reply(fight()))
    respx.get(DISPUTES).mock(return_value=httpx.Response(200, json={"items": [
        {"dispute_id": DISPUTE_ID, "update_time": paypal.data["update_time"],
         "status": "WAITING_FOR_SELLER_RESPONSE"}], "links": []}))
    client, _ = make_client(tmp_path)
    shipped_order(client)
    assert client.get("/api/disputes/sync/status").json()["last_synced_at"] is None

    with client:
        client.portal.call(client.app.state.auto_sync)  # one scheduler tick, no sleeping

    status = client.get("/api/disputes/sync/status").json()
    assert status["trigger"] == "auto" and status["last_synced_at"] and status["error"] is None
    assert status["counts"]["new"] == 1
    assert events(client, "ORDER1", "dispute_created")[0]["payload"]["source"] == "sync"
    # Missed webhook recovered, and the normal rules ran the AI for it.
    [rec] = events(client, "ORDER1", "ai_recommendation")
    assert rec["payload"]["trigger"] == "auto" and gemini.call_count == 1


@respx.mock
def test_sync_error_is_reported_in_status(tmp_path):
    setup_paypal()
    respx.get(DISPUTES).mock(return_value=httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"}))
    client, _ = make_client(tmp_path)
    assert client.post("/api/disputes/sync").status_code == 502
    status = client.get("/api/disputes/sync/status").json()
    assert status["error"] == "PayPal rejected the request (HTTP 500)" and not status["running"]


# ---- Readable errors and AI trigger -------------------------------------------

def test_paypal_errors_read_as_sentences():
    state = PayPalError(422, {"name": "UNPROCESSABLE_ENTITY", "debug_id": "d1", "details": [
        {"issue": "ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE"}]})
    assert state.readable == "PayPal didn't allow this step in the dispute's current state"
    assert state.debug_id == "d1"
    assert PayPalError(400, {"message": "Request is not well-formed."}).readable == \
        "Request is not well-formed."
    assert PayPalError(500, "oops").readable == "PayPal rejected the request (HTTP 500)"


@respx.mock
def test_regenerate_is_recorded_as_trigger(tmp_path):
    setup_paypal()
    DisputeOnPayPal(status="UNDER_REVIEW", dispute_life_cycle_stage="CHARGEBACK")
    gemini = respx.post(GEMINI).mock(return_value=ai_reply(fight()))
    client, _ = make_client(tmp_path)
    shipped_order(client)
    open_dispute(client)
    assert not gemini.called  # UNDER_REVIEW: no automatic run
    client.post(f"/api/disputes/{DISPUTE_ID}/recommendation")
    [rec] = events(client, "ORDER1", "ai_recommendation")
    assert rec["payload"]["trigger"] == "regenerate"


# ---- Sandbox: simulate PayPal asking the seller for evidence ----------------------

def under_review(tmp_path, **settings):
    setup_paypal()
    base = f"{DISPUTES}/{DISPUTE_ID}"
    paypal = DisputeOnPayPal(status="UNDER_REVIEW", dispute_life_cycle_stage="CHARGEBACK",
                             links=[{"rel": "self", "method": "GET", "href": base},
                                    {"rel": "provide_supporting_info", "method": "POST",
                                     "href": f"{base}/provide-supporting-info"}])
    client, _ = make_client(tmp_path, **settings)
    shipped_order(client)
    open_dispute(client)
    return client, paypal


@respx.mock
def test_sandbox_helper_is_404_in_live_mode(tmp_path):
    client, settings = make_client(tmp_path, paypal_env="live")
    # A real, known UNDER_REVIEW dispute, so only the live-mode guard can produce the 404.
    TimelineStore(settings.database_target).add("ORDER1", "dispute_created", {
        "dispute_id": DISPUTE_ID, "status": "UNDER_REVIEW", "update_time": "2026-10-27T00:00:00Z"})
    paypal_live = respx.route(host="api-m.paypal.com").mock(return_value=httpx.Response(200, json={}))
    resp = client.post(f"/api/disputes/{DISPUTE_ID}/sandbox/require-seller-evidence")
    assert resp.status_code == 404 and resp.json()["detail"] == "Not Found"
    assert not paypal_live.called  # not even a GET


@respx.mock
def test_sandbox_helper_409_unless_under_review(tmp_path):
    client, paypal = under_review(tmp_path)
    route = respx.post(REQUIRE).mock(return_value=httpx.Response(200, json={}))
    paypal.data["status"] = "WAITING_FOR_BUYER_RESPONSE"  # re-fetched state wins
    resp = client.post(f"/api/disputes/{DISPUTE_ID}/sandbox/require-seller-evidence")
    assert resp.status_code == 409 and not route.called


@respx.mock
def test_sandbox_helper_moves_dispute_and_lets_ai_run(tmp_path):
    client, paypal = under_review(tmp_path)
    gemini = respx.post(GEMINI).mock(return_value=ai_reply(fight()))
    base = f"{DISPUTES}/{DISPUTE_ID}"

    def paypal_moves_it(request):
        paypal.data.update(status="WAITING_FOR_SELLER_RESPONSE",
                           update_time="2026-10-27T06:00:00.000Z",
                           links=[{"rel": "provide_evidence", "method": "POST",
                                   "href": f"{base}/provide-evidence"}])
        return httpx.Response(200, json={"links": [{"rel": "self"}]})

    route = respx.post(REQUIRE).mock(side_effect=paypal_moves_it)
    assert not gemini.called  # nothing ran while PayPal was reviewing

    resp = client.post(f"/api/disputes/{DISPUTE_ID}/sandbox/require-seller-evidence")
    assert resp.status_code == 200 and resp.json() == {"status": "WAITING_FOR_SELLER_RESPONSE"}
    request = route.calls[0].request
    assert json.loads(request.content) == {"action": "SELLER_EVIDENCE"}  # require_evidence schema
    assert request.headers["content-type"] == "application/json"
    assert request.headers["PayPal-Request-Id"]

    [changed] = events(client, "ORDER1", "sandbox_status_changed")
    assert changed["payload"] == {"dispute_id": DISPUTE_ID, "action": "SELLER_EVIDENCE",
                                  "from_status": "UNDER_REVIEW"}
    state = events(client, "ORDER1", "dispute_updated")[-1]["payload"]
    assert state["status"] == "WAITING_FOR_SELLER_RESPONSE" and state["source"] == "sandbox"
    assert state["allowed_decisions"] == ["FIGHT"]
    [rec] = events(client, "ORDER1", "ai_recommendation")  # normal rules: now it may run
    assert rec["payload"]["trigger"] == "auto"


@respx.mock
def test_sandbox_helper_records_paypal_refusal(tmp_path):
    client, _ = under_review(tmp_path)
    respx.post(REQUIRE).mock(return_value=httpx.Response(422, json={
        "name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg-9",
        "details": [{"issue": "ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE"}]}))
    resp = client.post(f"/api/disputes/{DISPUTE_ID}/sandbox/require-seller-evidence")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "PayPal didn't allow this step in the dispute's current state"
    [failed] = events(client, "ORDER1", "sandbox_status_change_failed")
    assert failed["payload"]["debug_id"] == "dbg-9"
    assert events(client, "ORDER1", "sandbox_status_changed") == []
