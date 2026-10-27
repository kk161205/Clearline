"""OpenAI-compatible chat client for the dispute assistant, with a STICKY fallback.

Gemini is the primary provider and Groq the fallback. When Gemini fails for a reason that
looks like an outage or a bad key, we switch to Groq and stay there (stored in SQLite, so it
survives restarts) until someone calls POST /api/ai/reset. We never drift back automatically.

Every reply is requested as strict JSON and then validated again with pydantic here; the
provider's own schema enforcement is a convenience, not something we trust.
"""

import logging
import time
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


def _error_message(resp: httpx.Response) -> str:
    """Short provider error text for logs and the timeline (no request data echoed)."""
    try:
        data = resp.json()
        err = data[0] if isinstance(data, list) else data
        return str((err.get("error") or {}).get("message", ""))[:300]
    except (ValueError, AttributeError, IndexError):
        return resp.text[:300]
