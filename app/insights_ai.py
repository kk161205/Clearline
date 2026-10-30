"""Insights assistant (Day 8): AG Studio's agent framework on Clearline's own AI client.

AG Studio runs the agent loop in the seller's browser and calls POST /api/insights/ai once per
model turn. This module turns Studio's turn (conversation items + tool schemas) into an
OpenAI-compatible chat request for each provider, and the model's reply back into text and tool
calls for the browser.

Guardrails:
- One analyst agent, with read/query and add/configure-widget tools only (ALLOWED_TOOLS); any
  other tool is dropped from the request and refused in the reply. None of them can touch money,
  disputes or PayPal: Studio's tools only read and arrange the report.
- At most MAX_TURNS model turns per user question; then a polite "try a narrower question".
- Every message and tool result is PII-stripped (assistant.strip_pii) before it is forwarded.
- Gemini first, Groq per request only: this never reads or writes the sticky ai_state, which
  belongs to the dispute assistant.

Gemini 3 thought signatures: Gemini attaches a signature to the tool calls it makes and rejects
the next turn unless the same assistant message comes back with it. The browser only ever sees
our tool-call ids; the raw assistant message (with every field Gemini sent) stays here, in
SignatureCache, and is put back into the history before it goes to Gemini again.

Token budget: Groq's free tier allows 8,000 tokens a minute, so Groq gets the reduced tool set
(schema + query), a pruned query schema, a trimmed history and capped tool results.
"""

import base64
import copy
import json
import logging
import re
import time
import uuid
from collections import OrderedDict
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .ai import estimate_tokens  # noqa: F401  (re-exported for the tests)
from .assistant import strip_pii

log = logging.getLogger("clearline.insights")

MAX_TURNS = 6          # model turns for a question that only reads the data
MAX_WIDGET_TURNS = 8   # ...and for one that adds or changes widgets
WIDGET_TOOLS = {"add_widget", "position_widget", "configure_widget"}
WIDGET_WORDS = re.compile(r"\b(add|chart|graph|plot|widget|table|pie|bar|line|column|donut|"
                          r"visuali[sz]e|move|resize|rename|change)\b", re.IGNORECASE)
TOO_MANY_TURNS = ("That needs more steps than I can take for one question. Try a narrower "
                  "question, for example \"Which orders need me this week?\"")
ALLOWED_TOOLS = {"view_schema", "view_report", "view_page", "view_widget", "execute_query",
                 "add_widget", "position_widget", "configure_widget", "rename_thread"}
# Groq starts with these only (token budget); Gemini gets every allowed tool.
REDUCED_TOOLS = {"view_schema", "execute_query"}
SYSTEM = """You are the Insights analyst in Clearline, a seller's payments workspace. Answer
questions about the seller's orders, invoices, refunds and disputes from the report's data only:
call view_schema, then execute_query. You may add widgets and configure the widget you are given.
You cannot move money, reply to buyers, answer disputes or contact PayPal; those happen on the
Orders page. Be brief and use plain words. The data has no personal details.
Rules:
- "Needs me", "this week", "today", "overdue": filter on Act by, never on Deadline. Act by is
  set only on items that need the seller. This week = Act by on or before the end of this week;
  today = Act by on or before today; overdue = Act by before today. Today is {today}. If nothing
  matches, say so plainly.
- Money always has 2 decimals and the currency: "120.00 USD".
- For each item, give its Next action, and its Deadline when there is one."""

HISTORY_QUESTIONS = 3        # keep the last three questions (and what followed each)
MAX_TOOL_RESULT = 4000       # characters of one tool result sent to the model
MAX_STUDIO_GUIDANCE = 1200   # characters of Studio's per-turn instructions
MAX_TOOL_DESCRIPTION = 300   # characters of a tool's own description
GROQ_SCHEMA_DEPTH = 5        # Groq's query schema is pruned below this depth
TRUNCATED = "\n[truncated: the result was longer; ask a narrower question or add a limit]"
# Widget building: Gemini gets every tool, so it gets this too (Groq has no widget tools).
WIDGET_GUIDE = """Widgets: for charts and tables use Studio's built-in types (bar-chart-grouped,
column-chart-grouped, line-chart, pie-chart, donut-chart, grid, value). Use money-state only when
the user asks for money state or the clear line. Add the widget, then configure it once; put new
widgets below the report (yTrack 60). Example, "bar chart of refunded amount by buyer country":
1. view_schema {}
2. add_widget {"widgetId":"refunds-by-country","type":"bar-chart-grouped","xTrack":0,"yTrack":60,"xSpan":24,"ySpan":12}
3. configure_widget {"widgetId":"refunds-by-country","op":"patch","value":{"dataMapping":{"categoryKey":[{"id":"money.country"}],"valueKey":[{"id":"money.refunded","aggregation":"sum"}]},"format":{"title":{"enabled":true,"text":"Refunded by buyer country"}}}}
4. Answer in one sentence.
A step that fails lists "Errors to fix": fix exactly those and try once more. Health notes on a
step that worked (category_density, title_configured, data_mapping_complete right after you
configure) are advice: don't repeat steps for them."""
# Google's documented placeholder for a tool call whose signature was lost (as LiteLLM sends it).
SKIP_SIGNATURE = base64.b64encode(b"skip_thought_signature_validator").decode()
QUERY_EXAMPLES = """Examples (field ids come from view_schema):
{"query":{"variant":"aggregation","kind":"apiQuery","axes":[{"dimensions":[{"field":{"id":"money.next"}}]}],"measures":[{"field":{"id":"money.amount","aggregation":"sum"}}],"limit":{"count":50}}}
{"query":{"variant":"projection","kind":"apiQuery","projection":[{"field":{"id":"money.ref","aggregation":"first"}},{"field":{"id":"money.next","aggregation":"first"}}],"limit":{"count":50}}}"""

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


class SignatureCache:
    """Raw assistant tool-call messages as the provider sent them (with Gemini's thought
    signatures and any other extra fields), by tool-call id. In memory, 30 minutes, capped."""

    def __init__(self, ttl: float = 1800, cap: int = 500):
        self.ttl, self.cap = ttl, cap
        self.items: OrderedDict[str, tuple[float, str, dict]] = OrderedDict()

    def put(self, provider: str, raw: dict) -> None:
        for call in raw.get("tool_calls") or []:
            self.items[call["id"]] = (time.monotonic(), provider, raw)
            self.items.move_to_end(call["id"])
        while len(self.items) > self.cap:
            self.items.popitem(last=False)

    def get(self, call_id: str) -> tuple[str, dict] | None:
        hit = self.items.get(call_id)
        if not hit or time.monotonic() - hit[0] > self.ttl:
            self.items.pop(call_id, None)
            return None
        return hit[1], hit[2]


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


def simplify_schema(schema: Any, defs: dict | None = None, depth: int = 0, *,
                    descriptions: bool = True) -> dict:
    """Studio's JSON Schema 2020-12 -> the subset Gemini's OpenAI layer and Groq both accept:
    $ref inlined, const as enum, type lists and null branches folded, unknown keywords dropped.
    descriptions=False drops nested descriptions (Groq's token budget)."""
    if not isinstance(schema, dict):
        return {"type": "string"}
    if defs is None:
        defs = {**schema.get("$defs", {}), **schema.get("definitions", {})}
    if depth > 8:
        return {"type": "object"}
    nested = lambda s, d=1: simplify_schema(s, defs, depth + d, descriptions=descriptions)  # noqa: E731
    if "$ref" in schema:
        target = defs.get(str(schema["$ref"]).split("/")[-1], {})
        return nested({**target, **{k: v for k, v in schema.items() if k != "$ref"}})
    out: dict = {}
    if "const" in schema:
        out["enum"] = [schema["const"]]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            branches = [b for b in schema[key] if not (isinstance(b, dict) and b.get("type") == "null")]
            if len(branches) == 1:
                return nested({**branches[0], **{k: v for k, v in schema.items() if k not in ("anyOf", "oneOf")}})
            out["anyOf"] = [nested(b) for b in branches]
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
            out["properties"] = {k: nested(v) for k, v in value.items()}
        elif key == "items":
            out["items"] = nested(value)
        elif key == "enum" and isinstance(value, list):
            out["enum"] = [v for v in value if v is not None]
        elif key == "description":
            if descriptions or depth == 0:
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


def prune_schema(schema: dict, limit: int, depth: int = 0) -> dict:
    """Below `limit` levels, keep only a node's type (and enum): the model learns the deep shape
    from the examples, and Studio validates the real arguments in the browser anyway."""
    if not isinstance(schema, dict):
        return schema
    if depth >= limit:
        out = {"type": schema.get("type") or ("object" if "properties" in schema else "string")}
        if "enum" in schema:
            out["enum"] = schema["enum"]
        return out
    out = {}
    for key, value in schema.items():
        if key == "properties":
            out[key] = {k: prune_schema(v, limit, depth + 1) for k, v in value.items()}
        elif key == "items":
            out[key] = prune_schema(value, limit, depth + 1)
        elif key == "anyOf":
            branches = [prune_schema(b, limit, depth + 1) for b in value]
            unique = [b for i, b in enumerate(branches) if b not in branches[:i]]
            if len(unique) == 1:
                out.update(unique[0])
            else:
                out[key] = unique
        else:
            out[key] = value
    return out


def discriminators(schema: dict) -> list[str]:
    """Discriminated unions in a tool's parameters (e.g. query.variant), made explicit: every
    branch keeps its discriminator required, and the tool description names the values."""
    notes = []
    for name, prop in (schema.get("properties") or {}).items():
        branches = [b for b in prop.get("anyOf") or [] if isinstance(b, dict) and b.get("properties")]
        if len(branches) < 2:
            continue
        common = set.intersection(*(set(b["properties"]) for b in branches))
        for key in sorted(common):
            values = [b["properties"][key].get("enum") for b in branches]
            if all(v and len(v) == 1 for v in values):
                for b in branches:
                    if key not in b.setdefault("required", []):
                        b["required"].insert(0, key)
                notes.append(f"{name}.{key} is required; one of: {', '.join(str(v[0]) for v in values)}.")
    return notes


def chat_tools(tools: list[dict], provider: str = "gemini") -> list[dict]:
    """Studio's tool schemas, allowed tools only, as chat/completions function tools. Groq gets
    the reduced set, no nested descriptions and a pruned query schema with examples."""
    groq = provider == "groq"
    out = []
    for t in tools:
        if not isinstance(t, dict) or t.get("name") not in (REDUCED_TOOLS if groq else ALLOWED_TOOLS):
            continue
        params = simplify_schema(t.get("parameters") or t.get("schema") or {"type": "object"}, descriptions=not groq)
        notes = discriminators(params)
        if groq:
            params = prune_schema(params, GROQ_SCHEMA_DEPTH)
        description = str(t.get("description") or "")[:MAX_TOOL_DESCRIPTION]
        if notes:
            description += "\n" + " ".join(notes)
        if groq and t["name"] == "execute_query":
            description += "\n" + QUERY_EXAMPLES
        out.append({"type": "function", "function": {"name": t["name"], "description": description, "parameters": params}})
    return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("text"))


def recent_items(items: list[dict]) -> list[dict]:
    """The last HISTORY_QUESTIONS questions and everything after them. Cutting at a question
    keeps every tool call together with its result."""
    asked = [i for i, it in enumerate(items)
             if it.get("type") == "message" and it.get("kind") == "input" and it.get("role") == "user"]
    return items[asked[-HISTORY_QUESTIONS]:] if len(asked) > HISTORY_QUESTIONS else items


def chat_messages(turn: InsightsTurn) -> list[dict]:
    """Studio conversation items -> provider-neutral chat messages, PII-stripped and trimmed.
    Consecutive tool calls become one assistant message, as chat/completions expects; the
    provider-specific form (signatures for Gemini) is made by for_provider()."""
    guidance = clean(turn.instructions)[:MAX_STUDIO_GUIDANCE] if turn.instructions else ""
    system = SYSTEM.replace("{today}", date.today().isoformat())
    messages: list[dict] = [{"role": "system", "content": system + (f"\n\nFor this turn: {guidance}" if guidance else "")}]
    for item in recent_items(turn.items):
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
                             "content": tool_result(item.get("output"))})
        # reasoning items aren't replayed
    return messages


def _parsed(output: Any) -> dict:
    try:
        data = json.loads(output) if isinstance(output, str) else output
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def issue_messages(output: Any) -> list[str]:
    """The issues in a Studio tool result as "code: description" text. Studio puts them at the
    top level or under data; they name what is wrong (a field, a missing mapping), not data."""
    data = _parsed(output)
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    out = []
    for i in data.get("issues") or nested.get("issues") or []:
        if isinstance(i, dict):
            text = ": ".join(filter(None, [i.get("code"), i.get("description") or i.get("message")]))
        else:
            text = str(i)
        out.append(text[:300])
    return out


def tool_result(output: Any) -> str:
    """A tool result for the model: PII-stripped, Studio's issues first (so truncation can't
    drop them), capped at MAX_TOOL_RESULT. A failed step's issues are errors to fix; on a step
    that worked they are Studio's health notes, advice the model shouldn't chase."""
    issues = issue_messages(output)
    text = clean(output, tool_output=True)
    if issues:
        failed = _parsed(output).get("success") is False
        label = ("Errors to fix: " if failed
                 else "Health notes (advice only, nothing to fix unless the user asks): ")
        text = label + clean("; ".join(issues)) + "\n" + text
    return text[:MAX_TOOL_RESULT] + TRUNCATED if len(text) > MAX_TOOL_RESULT else text


def turn_cap(turn: InsightsTurn) -> int:
    """8 model turns for a question that adds or changes widgets (asked for in words, or
    already using widget tools), 6 for one that only reads the data."""
    items = turn.items
    asked = [i for i, it in enumerate(items)
             if it.get("type") == "message" and it.get("kind") == "input" and it.get("role") == "user"]
    current = items[asked[-1]:] if asked else items
    question = _text(current[0].get("content")) if asked else ""
    builds = any(it.get("type") == "function_call" and it.get("name") in WIDGET_TOOLS for it in current)
    return MAX_WIDGET_TURNS if builds or WIDGET_WORDS.search(question) else MAX_TURNS


def turn_summary(turn: InsightsTurn) -> tuple[list[str], int]:
    """For the logs: the tools called since the question (names only) and how many issues
    the latest results reported (a count, no text)."""
    items = turn.items
    asked = [i for i, it in enumerate(items)
             if it.get("type") == "message" and it.get("kind") == "input" and it.get("role") == "user"]
    current = items[asked[-1]:] if asked else items
    names = [str(it.get("name")) for it in current if it.get("type") == "function_call"]
    last_results = []
    for it in reversed(current):
        if it.get("type") != "function_call_output":
            break
        last_results.append(it)
    return names, sum(len(issue_messages(it.get("output"))) for it in last_results)


def standard_call(call: dict) -> dict:
    """A tool call with only the standard chat/completions fields (no provider extras)."""
    fn = call.get("function") or {}
    return {"id": call["id"], "type": "function",
            "function": {"name": fn.get("name"), "arguments": fn.get("arguments") or "{}"}}


def for_provider(messages: list[dict], provider: str, cache: SignatureCache) -> list[dict]:
    """The history as `provider` must see it. For Gemini, each assistant tool-call message is
    replaced by the raw message Gemini sent (its thought signatures intact); on a cache miss,
    Google's skip value goes on the first call. Anything else gets standard fields only.
    Gemini also gets the widget guide, since only Gemini gets the widget tools."""
    out = []
    for m in messages:
        if m["role"] == "system" and provider == "gemini" and not out:
            out.append({**m, "content": m["content"] + "\n" + WIDGET_GUIDE})
            continue
        if not m.get("tool_calls"):
            out.append(dict(m))
            continue
        ids = [c["id"] for c in m["tool_calls"]]
        hit = cache.get(ids[0])
        if provider == "gemini":
            if hit and hit[0] == "gemini" and [c["id"] for c in hit[1].get("tool_calls") or []] == ids:
                out.append({"role": "assistant", **copy.deepcopy(hit[1])})  # raw, every field kept
                continue
            log.warning("Insights assistant: no thought signature cached for a Gemini tool call; "
                        "sending Google's skip value")
            calls = [standard_call(c) for c in m["tool_calls"]]
            calls[0]["extra_content"] = {"google": {"thought_signature": SKIP_SIGNATURE}}
            out.append({"role": "assistant", "content": m.get("content"), "tool_calls": calls})
        else:
            out.append({"role": "assistant", "content": m.get("content"),
                        "tool_calls": [standard_call(c) for c in m["tool_calls"]]})
    return out


ALREADY_HAVE = ("\n[You already have this result: the same call ran earlier for this question. "
                "Use it instead of calling again.]")


def canonical_call(name: str, arguments: Any) -> str:
    """A tool call's identity: its name and its arguments with keys sorted and spacing removed."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        args = arguments
    return f"{name}:{json.dumps(args or {}, sort_keys=True, separators=(',', ':'), default=str)}"


def earlier_results(turn: InsightsTurn) -> dict[str, str]:
    """The results of the tool calls already made for the latest question, by canonical call."""
    items = turn.items
    asked = [i for i, it in enumerate(items)
             if it.get("type") == "message" and it.get("kind") == "input" and it.get("role") == "user"]
    calls, results = {}, {}
    for item in items[asked[-1] if asked else 0:]:
        if item.get("type") == "function_call":
            calls[str(item.get("callId"))] = canonical_call(str(item.get("name")), item.get("arguments"))
        elif item.get("type") == "function_call_output" and str(item.get("callId")) in calls:
            results[calls[str(item.get("callId"))]] = tool_result(item.get("output"))
    return results


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
    """The browser's response: text and (allowed) tool calls. Ids and arguments only, never a
    provider's extra fields such as thought signatures."""
    calls, refused = [], []
    for c in tool_calls:
        (calls if c["name"] in ALLOWED_TOOLS else refused).append(
            {"id": c["id"], "name": c["name"], "arguments": c.get("arguments") or "{}"})
    if refused:
        text = (text + "\n\n" if text else "") + "I can only read the report and arrange widgets here."
    return {"id": f"turn-{uuid.uuid4().hex[:12]}", "text": clean(text), "tool_calls": calls,
            "provider": provider, "model": model}
