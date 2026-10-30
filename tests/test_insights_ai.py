"""Day 8: the Insights assistant (AG Studio's agent framework on our own AI client). The AI is
always mocked with respx: no live AI calls."""

import base64
import json
import logging
import re
import shutil
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from app.ai import AIError
from app.assistant import strip_pii
from app.insights_ai import (ALLOWED_TOOLS, ALREADY_HAVE, MAX_TURNS, SKIP_SIGNATURE, TOO_MANY_TURNS,
                             TRUNCATED, InsightsTurn, SignatureCache, canonical_call, chat_messages,
                             chat_tools, clean, estimate_tokens, for_provider, simplify_schema,
                             tool_result, turn_cap)
from app.logsafe import short_reason
from app.logsafe import safe
from app.paypal import PayPalError
from app.store import TimelineStore
from tests.support import GEMINI, GROQ, make_client
from tests.test_dashboard import mixed  # noqa: F401  (fixture)
from tests.test_insights import insights_view

NODE = shutil.which("node")
ROOT = Path(__file__).parent.parent

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
    store = TimelineStore(settings.database_target)
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
    store = TimelineStore(settings.database_target)
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


# ---- Answer quality: the guidance the model gets -------------------------------------------

# The live case: six rows, three of them waiting for the buyer (they don't need the seller).
SIX_ROWS = "\n".join([
    "| Order | Next action | Needs you | Deadline | Amount |",
    "| …AGYPCZ | Respond by 3 Nov | true | 2026-11-03 | 85.00 |",
    "| …5K6YKJ | Buyer says it hasn't arrived | true |  | 120.00 |",
    "| Invoice 0104 | Invoice overdue 7 days | true | 2026-10-22 | 650.00 |",
    "| …R3EPVH | Waiting for the buyer | false |  | 60.00 |",
    "| …ZY19KR | Waiting for the buyer | false |  | 120.00 |",
    "| …DTGDUK | Waiting for the buyer | false |  | 60.00 |",
])


@respx.mock
def test_needs_you_guidance_and_today_are_sent_with_the_question(tmp_path):
    gemini = respx.post(GEMINI).mock(return_value=answer("Three items need you."))
    client, _ = make_client(tmp_path)
    turn = then_result({"tool_calls": [{"id": "q1", "name": "execute_query", "arguments": "{}"}]}, output=SIX_ROWS)
    assert client.post("/api/insights/ai", json=turn).status_code == 200
    body = sent(gemini)
    system = body["messages"][0]["content"]
    assert "filter on Act by, never on Deadline" in system
    assert "Act by is\n  set only on items that need the seller" in system and "say so plainly" in system
    assert '"120.00 USD"' in system and "its Next action, and its Deadline when there is one" in system
    assert f"Today is {date.today().isoformat()}." in system
    tool = next(m for m in body["messages"] if m["role"] == "tool")
    assert tool["content"] == SIX_ROWS  # the rows, dates and amounts reach the model intact


# ---- Repeated tool calls within one question ---------------------------------------------

def asked_once(args='{"query": {"variant": "projection", "kind": "apiQuery"}}', result="RESULT-A", rounds=1):
    """A question whose execute_query already ran (`rounds` rounds of it), with its result."""
    turn = question()
    for i in range(rounds):
        turn["items"] += [
            {"kind": "output", "type": "function_call", "callId": f"q{i}", "name": "execute_query",
             "arguments": args, "status": "completed"},
            {"kind": "input", "type": "function_call_output", "callId": f"q{i}", "output": result}]
    return turn


@respx.mock
def test_a_repeated_query_is_answered_from_the_earlier_result_not_run_again(tmp_path, caplog):
    same_call_reordered = '{"query":{"kind":"apiQuery","variant":"projection"}}'  # same, keys in another order
    gemini = respx.post(GEMINI).mock(side_effect=[answer(calls=[("execute_query", same_call_reordered)]),
                                                  answer("Done, from the earlier result.")])
    client, _ = make_client(tmp_path)
    caplog.set_level("INFO")
    out = client.post("/api/insights/ai", json=asked_once()).json()
    assert out["text"] == "Done, from the earlier result." and out["tool_calls"] == []  # the browser runs nothing
    assert gemini.call_count == 2
    tool = [m for m in sent(gemini, 1)["messages"] if m["role"] == "tool"][-1]
    assert tool["content"] == "RESULT-A" + ALREADY_HAVE
    assert "1 repeated tool call(s) answered from earlier results" in caplog.text


@respx.mock
def test_a_new_query_still_goes_to_the_browser(tmp_path):
    respx.post(GEMINI).mock(return_value=answer(calls=[("execute_query", '{"query":{"variant":"aggregation"}}')]))
    client, _ = make_client(tmp_path)
    out = client.post("/api/insights/ai", json=asked_once()).json()
    assert [c["name"] for c in out["tool_calls"]] == ["execute_query"]


@respx.mock
def test_answering_a_repeat_counts_toward_the_turn_cap(tmp_path):
    gemini = respx.post(GEMINI).mock(return_value=answer(calls=[("execute_query", '{"query": {"variant": "projection", "kind": "apiQuery"}}')]))
    client, _ = make_client(tmp_path)
    out = client.post("/api/insights/ai", json=asked_once(rounds=MAX_TURNS - 1)).json()
    assert out["text"] == TOO_MANY_TURNS and gemini.call_count == 1  # the repeat used the last turn


def test_canonical_calls_ignore_key_order_and_spacing():
    assert canonical_call("q", '{"b": 1, "a": [1, 2]}') == canonical_call("q", '{"a":[1,2],"b":1}')
    assert canonical_call("q", '{"a": 1}') != canonical_call("q", '{"a": 2}')
    assert canonical_call("q", "{}") != canonical_call("r", "{}")


# ---- Logs: counts, ids and statuses only -----------------------------------------------

@respx.mock
def test_ai_failures_are_logged_without_provider_text(tmp_path, caplog):
    echo = "Invalid value at 'messages[1]': Which orders for jane.buyer@example.com"
    respx.post(GEMINI).mock(return_value=httpx.Response(400, json={"error": {"message": echo}}))
    respx.post(GROQ).mock(return_value=httpx.Response(400, json={"error": {"message": echo}}))
    client, _ = make_client(tmp_path)
    caplog.set_level("INFO")
    assert client.post("/api/insights/ai", json=question("Orders for jane.buyer@example.com?")).status_code == 502
    assert "Insights assistant: Gemini failed (gemini: HTTP 400)" in caplog.text
    assert "jane" not in caplog.text and "Which orders" not in caplog.text and "Invalid value" not in caplog.text


def test_safe_error_text_keeps_status_name_and_debug_id_only():
    err = PayPalError(400, {"name": "INVALID_REQUEST", "debug_id": "f0e1", "message": "bad",
                            "details": [{"field": "/primary_recipients/0/email", "value": "client@acme.example"}]})
    assert safe(err) == "HTTP 400 INVALID_REQUEST debug_id f0e1"
    assert safe(AIError("groq", "HTTP 400: Tool call validation failed: /query secret", switch=False)) == "groq: HTTP 400"
    assert safe(AIError("gemini", "timeout", switch=False)) == "gemini: timeout"
    assert safe(httpx.ConnectTimeout("to https://api.example/x?key=SECRET")) == "ConnectTimeout"


def test_logging_is_configured_once_at_the_chosen_level(tmp_path):
    uvicorn = logging.getLogger("uvicorn")
    before = (uvicorn.level, list(uvicorn.handlers))
    make_client(tmp_path, log_level="WARNING")
    logger = logging.getLogger("clearline")
    assert logger.level == logging.WARNING
    make_client(tmp_path, log_level="INFO")
    assert logger.level == logging.INFO
    assert sum(h.get_name() == "clearline-console" for h in logger.handlers) <= 1  # never added twice
    assert logger.propagate  # pytest's caplog (and any root handler) still sees the records
    assert (uvicorn.level, list(uvicorn.handlers)) == before  # uvicorn's loggers are untouched


def test_no_log_call_formats_a_raw_error_or_content():
    """Every log call passes errors through safe() and logs counts, never problem lists."""
    checked = 0
    for path in (ROOT / "app").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for call in re.findall(r"log\.(?:info|warning|error)\((.*?)\)\n", text, re.S):
            args = [a.strip() for a in call.split(",")]
            assert not {"err", "err.reason", "problems", "messages", "body"} & set(args), f"{path.name}: {call}"
            checked += 1
    assert checked >= 15  # the scan really found the log calls


# ---- strip_pii, on both AI paths -----------------------------------------------------------

PII_TEXT = ("Write to jane.buyer@example.com or call +91 98765 43210, card 4111111111111111, "
            "see https://evil.example/x on 2026-10-29")


@pytest.mark.parametrize("path", ["dispute assistant", "insights"])
def test_strip_pii_on_both_ai_paths(path):
    out = strip_pii(PII_TEXT) if path == "dispute assistant" else clean(PII_TEXT)
    for gone in ("jane.buyer@example.com", "98765 43210", "4111111111111111", "https://evil.example/x"):
        assert gone not in out, (path, gone)
    if path == "insights":
        assert "2026-10-29" in out  # dates are data, kept on the Insights path
    assert clean(PII_TEXT, tool_output=True).count("2026-10-29") == 1


# ---- This round: needs-you wording, widget building, turn caps, issues, per-turn logs -------

def test_needs_me_questions_filter_on_act_by():
    system = chat_messages(InsightsTurn(**question()))[0]["content"]
    assert "This week = Act by on or before the end of this week" in system
    assert "overdue = Act by before today" in system


def test_widget_guide_with_the_bar_chart_example_goes_to_gemini_only():
    messages = chat_messages(InsightsTurn(**question("Add a bar chart of refunded amount by buyer country")))
    gemini = for_provider(messages, "gemini", SignatureCache())[0]["content"]
    groq = for_provider(messages, "groq", SignatureCache())[0]["content"]
    assert "Use money-state only when" in gemini and '"type":"bar-chart-grouped"' in gemini
    assert '"valueKey":[{"id":"money.refunded","aggregation":"sum"}]' in gemini
    assert "Health notes on a" in gemini
    assert "bar-chart-grouped" not in groq  # Groq has no widget tools: no tokens spent on it


def widget_rounds(n, text="Add a bar chart of refunded amount by buyer country"):
    turn = question(text)
    for i in range(n):
        turn["items"] += [
            {"kind": "output", "type": "function_call", "callId": f"w{i}", "name": "view_widget",
             "arguments": json.dumps({"widgetId": f"w{i}"}), "status": "completed"},
            {"kind": "input", "type": "function_call_output", "callId": f"w{i}", "output": "ok"}]
    return turn


@respx.mock
def test_widget_questions_get_eight_turns_plain_ones_six(tmp_path):
    gemini = respx.post(GEMINI).mock(return_value=answer("ok"))
    client, _ = make_client(tmp_path)
    assert client.post("/api/insights/ai", json=widget_rounds(7)).json()["text"] == "ok"  # turn 8 of 8
    assert client.post("/api/insights/ai", json=widget_rounds(8)).json()["text"] == TOO_MANY_TURNS
    plain = widget_rounds(6, text="Which orders need me this week?")
    assert client.post("/api/insights/ai", json=plain).json()["text"] == TOO_MANY_TURNS  # still 6
    assert turn_cap(InsightsTurn(**widget_rounds(0, "Which orders need me?"))) == 6
    assert gemini.call_count == 1


@respx.mock
def test_studio_issues_reach_the_model_first_and_labelled(tmp_path):
    gemini = respx.post(GEMINI).mock(return_value=answer("ok"))
    client, _ = make_client(tmp_path)
    failed = json.dumps({"success": False, "response": "x" * 6000, "issues": [
        {"code": "unknown_field", "description": "Field money.cntry does not exist"}]})
    turn = then_result({"tool_calls": [{"id": "c1", "name": "configure_widget", "arguments": "{}"}]}, output=failed)
    client.post("/api/insights/ai", json=turn)
    tool = next(m for m in sent(gemini)["messages"] if m["role"] == "tool")["content"]
    assert tool.startswith("Errors to fix: unknown_field: Field money.cntry does not exist")
    assert tool.endswith(TRUNCATED)  # long, yet the issue survived the cap
    advice = tool_result(json.dumps({"success": True, "data": {"issues": [
        {"code": "data_mapping_complete", "description": "One or more required data fields are not mapped"}]}}))
    assert advice.startswith("Health notes (advice only, nothing to fix unless the user asks): data_mapping_complete")


@respx.mock
def test_each_turn_logs_tool_names_and_issue_counts_only(tmp_path, caplog):
    respx.post(GEMINI).mock(return_value=answer(calls=[("configure_widget", '{"secretArg": "money.refunded"}')]))
    client, _ = make_client(tmp_path)
    caplog.set_level("INFO")
    issue = json.dumps({"success": False, "issues": [{"code": "bad", "description": "private detail"}]})
    turn = then_result({"tool_calls": [{"id": "c1", "name": "add_widget", "arguments": '{"widgetId": "w"}'}]}, output=issue)
    turn["items"][0]["content"] = [{"type": "text", "text": "Add a chart"}]
    client.post("/api/insights/ai", json=turn)
    line = next(r.getMessage() for r in caplog.records if "Insights assistant: turn" in r.getMessage())
    assert line == ("Insights assistant: turn 2 of 8 on gemini; tools so far: add_widget; "
                    "issues in the last results: 1; next: configure_widget")
    assert "secretArg" not in caplog.text and "private detail" not in caplog.text and "Add a chart" not in caplog.text


def test_provider_switch_reason_is_a_status_and_class_only():
    def reason(text):
        return short_reason(AIError("gemini", text, switch=True))
    assert reason("HTTP 429: Resource exhausted for project 123 key AIza...") == "HTTP 429 rate_limited"
    assert reason("HTTP 401 (bad or missing API key)") == "HTTP 401 auth_error"
    assert reason("HTTP 503: upstream text") == "HTTP 503 server_error"
    assert reason("timeout") == "timeout" and reason("invalid JSON after retry: 3 errors") == "invalid_reply"
    assert reason("GEMINI_API_KEY is not set") == "missing_key"
