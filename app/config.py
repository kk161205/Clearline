from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    paypal_client_id: str
    paypal_client_secret: str
    paypal_env: str = "sandbox"
    database_path: str = "clearline.db"
    # Postgres (e.g. Neon's connection string, with sslmode=require) instead of the SQLite file above.
    # Unset = SQLite at database_path (local dev). Never logged.
    database_url: str | None = Field(default=None, repr=False)
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
    # Signs buyer links (/o/{token}). If empty, a random one is made at startup, so buyer links
    # stop working after a restart. (Seller logins are accounts in the database, not settings.)
    app_secret: str | None = None
    # Behind a proxy that sets X-Forwarded-For / X-Forwarded-Proto (Render): trust those headers
    # for the client IP and HTTPS. Off by default, so a direct client can't spoof them.
    trust_proxy: bool = False
    # Shown to buyers on their order page ("what is this charge?").
    seller_display_name: str = "Demo Design Studio"
    # Day 5: the seller's country (ISO code). "IN" shows the India export paperwork checklist.
    seller_country: str = "IN"
    # Day 8: AG Studio licence key for /insights. Optional: without it AG Studio shows its
    # watermark. Sent only to the logged-in seller's browser at runtime; never commit it.
    ag_studio_license_key: str | None = None
    # Level for Clearline's own logs (uvicorn's are untouched). Lines carry counts, ids and
    # statuses only, so they're safe to ship to a log service.
    log_level: str = "INFO"
    # No longer used: seller logins are accounts and agent keys live in Settings -> Agent access.
    # Accepted so an old .env still starts; the app warns (by name, never the value) and ignores
    # them. Not shown in repr.
    legacy_seller_password: str | None = Field(default=None, validation_alias="SELLER_PASSWORD", repr=False)
    legacy_merchant_api_key: str | None = Field(default=None, validation_alias="MERCHANT_API_KEY", repr=False)

    @property
    def database_target(self) -> str:
        """What TimelineStore opens: the Postgres URL if set, else the SQLite file."""
        return self.database_url or self.database_path

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
