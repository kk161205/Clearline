"""Insights assistant (Day 8): AG Studio's agent framework on Clearline's own AI client.

AG Studio runs the agent loop in the seller's browser and calls POST /api/insights/ai once per
model turn. This module turns Studio's turn (conversation items + tool schemas) into an
OpenAI-compatible chat request, and the model's reply back into text and tool calls.

Guardrails:
- One analyst agent, with read/query and add/configure-widget tools only (ALLOWED_TOOLS); any
  other tool is dropped from the request and refused in the reply. None of them can touch money,
  disputes or PayPal: Studio's tools only read and arrange the report.
- At most MAX_TURNS model turns per user question; then a polite "try a narrower question".
- Every message and tool result is PII-stripped (assistant.strip_pii) before it is forwarded.
- Gemini first, Groq per request only: this never reads or writes the sticky ai_state, which
  belongs to the dispute assistant.
"""

import json
import re
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .assistant import strip_pii

MAX_TURNS = 6
TOO_MANY_TURNS = ("That needs more steps than I can take for one question. Try a narrower "
                  "question, for example \"Which orders need me this week?\"")
ALLOWED_TOOLS = {"view_schema", "view_report", "view_page", "view_widget", "execute_query",
                 "add_widget", "position_widget", "configure_widget", "rename_thread"}
# If both providers reject the full tool list, the turn is retried with these only.
REDUCED_TOOLS = {"view_schema", "execute_query"}
SYSTEM = """You are the Insights analyst inside Clearline, a seller's payments workspace.
You answer questions about the seller's orders, invoices, refunds and disputes using ONLY the
report's data, through your tools: call view_schema before writing a query, then execute_query.
You may add widgets to the report and configure the widget you are given. You cannot move money,
reply to buyers, answer disputes or contact PayPal: if asked, say those actions happen on the
Orders page. Be brief and use plain words. Amounts are in the currency named in the report.
The data has no personal details; never ask for any."""

ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b")
EPOCH = re.compile(r"\b1\d{9}(?:\d{3})?\b")  # epoch seconds or ms, as Studio may return dates
SCHEMA_KEYS = {"type", "properties", "required", "items", "enum", "description", "minimum",
               "maximum", "minItems", "maxItems", "minLength", "maxLength", "nullable", "anyOf"}


class InsightsTurn(BaseModel):
    """One model turn as the browser adapter sends it (Studio's own shapes, passed through)."""
    model_config = ConfigDict(extra="ignore")
    instructions: str | None = Field(default=None, max_length=40_000)
    items: list[dict] = Field(default_factory=list, max_length=400)
    tools: list[dict] = Field(default_factory=list, max_length=40)
    tool_choice: Any = None


def clean(text: Any, *, tool_output: bool = False) -> str:
    """strip_pii, keeping ISO dates (and, in tool results, epoch timestamps) intact: the data is
    already PII-free, and the stripper would otherwise blank every date as a "phone number"."""
    text = text if isinstance(text, str) else json.dumps(text, default=str)
    kept: list[str] = []

    def keep(m: re.Match) -> str:
        kept.append(m.group(0))
        return f"⟦{len(kept) - 1}⟧"

    text = ISO_DATE.sub(keep, text)
    if tool_output:
        text = EPOCH.sub(keep, text)
    text = strip_pii(text)
    return re.sub("⟦(\\d+)⟧", lambda m: kept[int(m.group(1))], text)


def simplify_schema(schema: Any, defs: dict | None = None, depth: int = 0) -> dict:
    """Studio's JSON Schema 2020-12 -> the subset Gemini's OpenAI layer and Groq both accept:
    $ref inlined, const as enum, type lists and null branches folded, unknown keywords dropped."""
    if not isinstance(schema, dict):
        return {"type": "string"}
    if defs is None:
        defs = {**schema.get("$defs", {}), **schema.get("definitions", {})}
    if depth > 8:
        return {"type": "object"}
    if "$ref" in schema:
        target = defs.get(str(schema["$ref"]).split("/")[-1], {})
        return simplify_schema({**target, **{k: v for k, v in schema.items() if k != "$ref"}}, defs, depth + 1)
    out: dict = {}
    if "const" in schema:
        out["enum"] = [schema["const"]]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            branches = [b for b in schema[key] if not (isinstance(b, dict) and b.get("type") == "null")]
            if len(branches) == 1:
                return simplify_schema({**branches[0], **{k: v for k, v in schema.items() if k not in ("anyOf", "oneOf")}}, defs, depth + 1)
            out["anyOf"] = [simplify_schema(b, defs, depth + 1) for b in branches]
    t = schema.get("type")
    if isinstance(t, list):
        types = [x for x in t if x != "null"]
        t = types[0] if types else "string"
    if t:
        out["type"] = t
    for key, value in schema.items():
        if key not in SCHEMA_KEYS or key in ("type", "anyOf"):
            continue
        if key == "properties" and isinstance(value, dict):
            out["properties"] = {k: simplify_schema(v, defs, depth + 1) for k, v in value.items()}
        elif key == "items":
            out["items"] = simplify_schema(value, defs, depth + 1)
        elif key == "enum" and isinstance(value, list):
            out["enum"] = [v for v in value if v is not None]
        elif key == "description":
            out["description"] = str(value)[:1000]
        else:
            out[key] = value
    if out.get("type") == "object" or "properties" in out:
        out.setdefault("type", "object")
        out.setdefault("properties", {})
        if "required" in out:
            out["required"] = [r for r in out["required"] if r in out["properties"]]
    if "anyOf" not in out and "type" not in out and "enum" not in out:
        out["type"] = "string"
    return out


def chat_tools(tools: list[dict]) -> list[dict]:
    """Studio's tool schemas, allowed tools only, as chat/completions function tools."""
    return [{"type": "function", "function": {
                "name": t["name"], "description": str(t.get("description") or "")[:1000],
                "parameters": simplify_schema(t.get("parameters") or t.get("schema") or {"type": "object"})}}
            for t in tools if isinstance(t, dict) and t.get("name") in ALLOWED_TOOLS]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("text"))


def chat_messages(turn: InsightsTurn) -> list[dict]:
    """Studio conversation items -> chat messages, PII-stripped. Consecutive tool calls become
    one assistant message, as chat/completions expects."""
    system = SYSTEM + (f"\n\nStudio's guidance for this turn:\n{clean(turn.instructions)}" if turn.instructions else "")
    messages: list[dict] = [{"role": "system", "content": system}]
    for item in turn.items:
        kind, typ = item.get("kind"), item.get("type")
        if typ == "message" and kind == "input":
            role = "user" if item.get("role") == "user" else "system"
            messages.append({"role": role, "content": clean(_text(item.get("content")))})
        elif typ == "message":  # the assistant's earlier answer
            messages.append({"role": "assistant", "content": clean(_text(item.get("content")))})
        elif typ == "function_call":
            call = {"id": str(item.get("callId")), "type": "function",
                    "function": {"name": str(item.get("name")), "arguments": item.get("arguments") or "{}"}}
            last = messages[-1]
            if last["role"] == "assistant" and "tool_calls" in last and not last.get("content"):
                last["tool_calls"].append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif typ == "function_call_output":
            messages.append({"role": "tool", "tool_call_id": str(item.get("callId")),
                             "content": clean(item.get("output"), tool_output=True)[:60_000]})
        # reasoning items aren't replayed
    return messages


def turns_used(turn: InsightsTurn) -> int:
    """Model turns already taken for the latest user question: one per round of tool calls."""
    rounds, in_round = 0, False
    for item in turn.items:
        if item.get("type") == "message" and item.get("kind") == "input" and item.get("role") == "user":
            rounds, in_round = 0, False
        elif item.get("type") == "function_call":
            if not in_round:
                rounds += 1
            in_round = True
        else:
            in_round = False
    return rounds


def reply(text: str, tool_calls: list[dict], provider: str | None, model: str | None) -> dict:
    """The adapter's response: text and (allowed) tool calls."""
    calls, refused = [], []
    for c in tool_calls:
        (calls if c["name"] in ALLOWED_TOOLS else refused).append(c)
    if refused:
        text = (text + "\n\n" if text else "") + "I can only read the report and arrange widgets here."
    return {"id": f"turn-{uuid.uuid4().hex[:12]}", "text": clean(text), "tool_calls": calls,
            "provider": provider, "model": model}
