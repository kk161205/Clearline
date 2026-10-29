"""OpenAI-compatible chat client for the dispute assistant, with a STICKY fallback.

Gemini is the primary provider and Groq the fallback. When Gemini fails for a reason that
looks like an outage or a bad key, we switch to Groq and stay there (stored in SQLite, so it
survives restarts) until someone calls POST /api/ai/reset. We never drift back automatically.

Every reply is requested as strict JSON and then validated again with pydantic here; the
provider's own schema enforcement is a convenience, not something we trust.
"""

import asyncio
import copy
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .store import TimelineStore

log = logging.getLogger("clearline.ai")

PROVIDERS = {
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key": "gemini_api_key", "model": "gemini_model", "label": "Gemini"},
    "groq": {"base_url": "https://api.groq.com/openai/v1",
             "key": "groq_api_key", "model": "groq_model", "label": "Groq"},
}


class AIError(Exception):
    """A provider call failed. `switch` says whether this justifies leaving Gemini."""

    def __init__(self, provider: str, reason: str, *, switch: bool, config_error: bool = False):
        self.provider = provider
        self.reason = reason
        self.switch = switch
        self.config_error = config_error
        self.busy = False            # rate-limited (HTTP 429)
        self.retry_after: float | None = None
        self.tool_validation = False  # the provider rejected the model's tool call
        super().__init__(f"{provider}: {reason}")


@dataclass
class AIResult:
    data: BaseModel
    provider: str
    model: str
    latency_ms: int
    prompt_version: str


class AIClient:
    def __init__(self, settings: Settings, store: TimelineStore,
                 http: httpx.AsyncClient | None = None):
        self.settings = settings
        self.store = store
        self.http = http or httpx.AsyncClient(timeout=settings.ai_timeout_seconds)

    @property
    def configured(self) -> bool:
        """True if at least one provider has a key, i.e. it's worth trying at all."""
        return any(getattr(self.settings, p["key"]) for p in PROVIDERS.values())

    def model_for(self, provider: str) -> str:
        return getattr(self.settings, PROVIDERS[provider]["model"])

    def status(self) -> dict:
        """Active provider and model for GET /api/ai/status (never includes keys)."""
        state = self.store.ai_state()
        return {**state, "model": self.model_for(state["active_provider"])}

    def reset(self) -> None:
        """Manually switch back to Gemini (the only way to leave Groq)."""
        previous = self.store.ai_state()["active_provider"]
        self.store.set_ai_provider("gemini", "manual reset")
        if previous != "gemini":
            self.store.add("other", "ai_provider_switched",
                           {"from": previous, "to": "gemini", "reason": "manual reset"})

    async def complete(self, messages: list[dict], *, schema_name: str, schema: dict,
                       model_cls: type[BaseModel], prompt_version: str,
                       allow_switch: bool = True) -> AIResult:
        """Run one structured request on the active provider, switching Gemini -> Groq for good
        if Gemini looks down or misconfigured. Raises AIError if the request can't be served.

        allow_switch=False (buyer summaries, invoice drafts, reminders, purpose suggestions):
        use the active provider only and raise on failure; only the dispute assistant may
        switch the app's provider."""
        provider = self.store.ai_state()["active_provider"]
        if not allow_switch:
            return await self._call_validated(provider, messages, schema_name, schema,
                                              model_cls, prompt_version)
        if provider == "gemini":
            try:
                return await self._call_validated("gemini", messages, schema_name, schema,
                                                  model_cls, prompt_version)
            except AIError as err:
                if not err.switch:
                    raise
                self._switch_to_groq(err)
        return await self._call_validated("groq", messages, schema_name, schema, model_cls,
                                          prompt_version)

    async def chat_tools(self, prepare, tool_choice=None) -> dict:
        """One tool-calling chat turn for the Insights assistant.

        prepare(provider) -> (messages, tools) builds each provider's own request (Gemini gets
        its thought signatures back and every tool; Groq gets the reduced, trimmed request).
        Returns {text, tool_calls, raw, provider, model}, where raw is the assistant message
        exactly as the provider sent it. Starts on Gemini every time and falls back to Groq for
        this request only; never reads or writes the sticky ai_state (that switch belongs to
        the dispute assistant), so the next request starts on Gemini again. Raises the last
        AIError (busy=True when the last provider was rate-limited)."""
        last: AIError | None = None
        for provider in ("gemini", "groq"):
            if not getattr(self.settings, PROVIDERS[provider]["key"]):
                continue
            messages, tools = prepare(provider)
            try:
                return await self._tools_turn(provider, messages, tools, tool_choice)
            except AIError as err:
                log.warning("Insights assistant: %s failed (%s)%s", PROVIDERS[provider]["label"],
                            err.reason, ", trying Groq for this request" if provider == "gemini" else "")
                last = err
        raise last or AIError("none", "no AI key configured", switch=False, config_error=True)

    async def _tools_turn(self, provider: str, messages: list[dict], tools: list[dict],
                          tool_choice) -> dict:
        """One provider, with at most one retry: after a short rate-limit wait (under 5 s), or
        after a rejected tool call (the model gets the validation error to correct itself)."""
        retried = False
        while True:
            try:
                return await self._post_tools(provider, messages, tools, tool_choice)
            except AIError as err:
                if retried:
                    raise
                if err.retry_after is not None and err.retry_after < 5:
                    await asyncio.sleep(err.retry_after)
                elif err.tool_validation:
                    messages = messages + [{"role": "user", "content":
                        "Your last tool call was rejected: " + err.reason[:400] + ". Call the tool again "
                        "with arguments that match its schema exactly (required fields included)."}]
                else:
                    raise
                retried = True

    async def _post_tools(self, provider: str, messages: list[dict], tools: list[dict],
                          tool_choice) -> dict:
        cfg = PROVIDERS[provider]
        body = {"model": self.model_for(provider), "messages": messages}
        if tools:
            body["tools"] = tools
            if tool_choice in ("auto", "none", "required"):
                body["tool_choice"] = tool_choice
        # Sizes only, never content.
        log.info("Insights assistant: %s request, about %d tokens (%d messages, %d tools)",
                 cfg["label"], estimate_tokens(body), len(messages), len(tools))
        try:
            resp = await self.http.post(f"{cfg['base_url']}/chat/completions", json=body,
                                        headers={"Authorization": f"Bearer {getattr(self.settings, cfg['key'])}"},
                                        timeout=self.settings.ai_timeout_seconds)
        except httpx.TimeoutException:
            raise AIError(provider, "timeout", switch=False)
        except httpx.HTTPError as err:
            raise AIError(provider, f"network error: {type(err).__name__}", switch=False)
        if resp.status_code >= 400:
            message = _error_message(resp)
            err = AIError(provider, f"HTTP {resp.status_code}: {message}", switch=False)
            if resp.status_code == 429:
                err.busy, err.retry_after = True, _retry_after(resp, message)
            # Includes 400 for a tool schema or thought signature this provider rejects.
            err.tool_validation = resp.status_code == 400 and (
                "tool_use_failed" in resp.text or "Tool call validation failed" in message)
            raise err
        try:
            raw = resp.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise AIError(provider, "unreadable reply", switch=False)
        raw = with_ids(raw)
        calls = [{"id": c["id"], "name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}
                 for c in raw.get("tool_calls") or [] if c.get("function")]
        return {"text": raw.get("content") or "", "tool_calls": calls, "raw": raw,
                "provider": provider, "model": self.model_for(provider)}

    def _switch_to_groq(self, err: AIError) -> None:
        if err.config_error:
            log.error("AI configuration error on Gemini (%s); switching to Groq permanently. "
                      "Fix the key, then POST /api/ai/reset.", err.reason)
        else:
            log.warning("Gemini failed (%s); switching to Groq permanently until "
                        "POST /api/ai/reset.", err.reason)
        self.store.set_ai_provider("groq", err.reason)
        self.store.add("other", "ai_provider_switched",
                       {"from": "gemini", "to": "groq", "reason": err.reason})

    async def _call_validated(self, provider: str, messages: list[dict], schema_name: str,
                              schema: dict, model_cls: type[BaseModel],
                              prompt_version: str) -> AIResult:
        """Call the provider; if the reply fails pydantic validation, retry once on the same
        provider with the error appended. Invalid twice counts as a provider failure."""
        started = time.monotonic()
        error: ValidationError | None = None
        for _ in range(2):
            content = await self._post(provider, messages, schema_name, schema)
            try:
                data = model_cls.model_validate_json(content)
            except ValidationError as err:
                error = err
                messages = messages + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "That reply was not valid for the schema: "
                     f"{err.errors(include_url=False)}. Reply again with JSON only, "
                     "matching the schema exactly."},
                ]
                continue
            return AIResult(data=data, provider=provider, model=self.model_for(provider),
                            latency_ms=round((time.monotonic() - started) * 1000),
                            prompt_version=prompt_version)
        raise AIError(provider, f"invalid JSON after retry: {error.error_count()} errors",
                      switch=True)

    async def _post(self, provider: str, messages: list[dict], schema_name: str,
                    schema: dict) -> str:
        """One chat/completions call. Returns the message content or raises AIError."""
        cfg = PROVIDERS[provider]
        key = getattr(self.settings, cfg["key"])
        if not key:
            raise AIError(provider, f"{cfg['key'].upper()} is not set", switch=True,
                          config_error=True)
        body = {
            "model": self.model_for(provider),
            "messages": messages,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": schema_name, "strict": True,
                                                "schema": schema}},
        }
        try:
            resp = await self.http.post(f"{cfg['base_url']}/chat/completions", json=body,
                                        headers={"Authorization": f"Bearer {key}"},
                                        timeout=self.settings.ai_timeout_seconds)
        except httpx.TimeoutException:
            raise AIError(provider, "timeout", switch=True)
        except httpx.HTTPError as err:
            raise AIError(provider, f"network error: {type(err).__name__}", switch=True)

        status = resp.status_code
        if status in (401, 403):
            raise AIError(provider, f"HTTP {status} (bad or missing API key)", switch=True,
                          config_error=True)
        if status == 429 or status >= 500:
            raise AIError(provider, f"HTTP {status}", switch=True)
        if status >= 400:
            # 400/404 mean our request is wrong (parameter, model id): a bug to fix, not an
            # outage, so we don't switch providers for it.
            raise AIError(provider, f"HTTP {status}: {_error_message(resp)}", switch=False)
        try:
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            return ""  # treated as invalid JSON by the caller


def estimate_tokens(body: dict) -> int:
    """A rough prompt size (about 4 characters a token), for the logs and the budget."""
    return len(json.dumps(body, separators=(",", ":"))) // 4


def with_ids(raw: dict) -> dict:
    """A provider's raw assistant message with an id on every tool call (kept unchanged
    otherwise), so the browser and the signature cache agree on which call is which."""
    raw = copy.deepcopy(raw)
    for call in raw.get("tool_calls") or []:
        if not call.get("id"):
            call["id"] = f"call-{uuid.uuid4().hex[:12]}"
    return raw


def _retry_after(resp: httpx.Response, message: str) -> float | None:
    """Seconds to wait from a 429: the Retry-After header, or "try again in 1.2s / 350ms"."""
    try:
        return float(resp.headers["retry-after"])
    except (KeyError, ValueError):
        pass
    m = re.search(r"try again in ([\d.]+)\s*(ms|s)", message, re.IGNORECASE)
    if not m:
        return None
    return float(m.group(1)) / (1000 if m.group(2).lower() == "ms" else 1)


def _error_message(resp: httpx.Response) -> str:
    """Short provider error text for logs and the timeline (no request data echoed)."""
    try:
        data = resp.json()
        err = data[0] if isinstance(data, list) else data
        return str((err.get("error") or {}).get("message", ""))[:300]
    except (ValueError, AttributeError, IndexError):
        return resp.text[:300]
