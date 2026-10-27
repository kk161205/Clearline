from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    paypal_client_id: str
    paypal_client_secret: str
    paypal_env: str = "sandbox"
    database_path: str = "clearline.db"
    # Webhooks (Day 2). The ID comes from Developer Dashboard -> your app -> Webhooks.
    paypal_webhook_id: str | None = None
    # Dev only: accept webhooks that fail signature verification (e.g. Webhook Simulator).
    allow_unverified_webhooks: bool = False
    # AI dispute assistant (Day 3). Gemini is primary; Groq is the sticky fallback.
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    ai_timeout_seconds: float = 20
    # Pull disputes from PayPal every N minutes as a safety net for missed webhooks; 0 = off.
    auto_sync_minutes: float = 3
    # Day 4: signs buyer links (/o/{token}) and seller sessions. If empty, a random one is made
    # at startup, so links and logins stop working after a restart.
    app_secret: str | None = None
    # Password for the seller pages and APIs. Empty = the seller side is locked (503).
    seller_password: str | None = None
    # Shown to buyers on their order page ("what is this charge?").
    seller_display_name: str = "Demo Design Studio"
    # Day 5: the seller's country (ISO code). "IN" shows the India export paperwork checklist.
    seller_country: str = "IN"

    @property
    def api_base(self) -> str:
        if self.paypal_env == "live":
            return "https://api-m.paypal.com"
        return "https://api-m.sandbox.paypal.com"

    @property
    def sdk_url(self) -> str:
        if self.paypal_env == "live":
            return "https://www.paypal.com/web-sdk/v6/core"
        return "https://www.sandbox.paypal.com/web-sdk/v6/core"


@lru_cache
def get_settings() -> Settings:
    return Settings()
