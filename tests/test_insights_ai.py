"""Day 8: the Insights assistant (AG Studio's agent framework on our own AI client). The AI is
always mocked with respx: no live AI calls."""

import json
import shutil

import httpx
import pytest
import respx

from app.insights_ai import ALLOWED_TOOLS, MAX_TURNS, TOO_MANY_TURNS, simplify_schema
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


@respx.mock
def test_schema_rejected_by_both_providers_falls_back_to_schema_and_query_tools(tmp_path):
    bad = httpx.Response(400, json={"error": {"message": "Invalid JSON payload: unknown field"}})
    gemini = respx.post(GEMINI).mock(side_effect=[bad, answer("Answered with the reduced tools.")])
    groq = respx.post(GROQ).mock(return_value=bad)
    client, _ = make_client(tmp_path)
    tools = TOOLS + [{"name": "add_widget", "description": "Adds.", "parameters": {"type": "object", "properties": {}}}]
    out = client.post("/api/insights/ai", json={**question(), "tools": tools}).json()
    assert out["text"] == "Answered with the reduced tools."
    assert [t["function"]["name"] for t in sent(gemini, 0)["tools"]] == ["view_schema", "execute_query", "add_widget"]
    assert [t["function"]["name"] for t in sent(gemini, 1)["tools"]] == ["view_schema", "execute_query"]
    assert groq.call_count == 1


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
    view = insights_view({**data, "assistant": {"reply": reply, "request": request, "states": states}})["assistant"]
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
