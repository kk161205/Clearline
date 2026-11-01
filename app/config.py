from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


MIN_APP_SECRET = 32
PAYPAL_ENV = "sandbox"  # "live" switches every PayPal URL below


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Required in production (see production_problems); blank locally is fine for UI checks.
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    # Local only: Neon (DATABASE_URL) is the source of truth on Render.
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
    groq_api_key: str | None = None
    # Signs buyer links (/o/{token}). If empty, a random one is made at startup, so buyer links
    # stop working after a restart. (Seller logins are accounts in the database, not settings.)
    app_secret: str | None = None
    # Day 8: AG Studio licence key for /insights. Optional: without it AG Studio shows its
    # watermark. Sent only to the logged-in seller's browser at runtime; never commit it.
    ag_studio_license_key: str | None = None
    # Level for Clearline's own logs (uvicorn's are untouched). Lines carry counts, ids and
    # statuses only, so they're safe to ship to a log service.
    log_level: str = "INFO"
    # Render sets these itself. RENDER switches production mode on, RENDER_EXTERNAL_URL is the
    # service's public address. APP_ENV=production turns production mode on anywhere else.
    render: str | None = None
    render_external_url: str | None = None
    app_env: str = "development"
    # Overrides RENDER_EXTERNAL_URL (a custom domain, say) for the absolute links we generate.
    public_base_url: str | None = None
    # No longer used: seller logins are accounts and agent keys live in Settings -> Agent access.
    # Accepted so an old .env still starts; the app warns (by name, never the value) and ignores
    # them. Not shown in repr.
    legacy_seller_password: str | None = Field(default=None, validation_alias="SELLER_PASSWORD", repr=False)
    legacy_merchant_api_key: str | None = Field(default=None, validation_alias="MERCHANT_API_KEY", repr=False)
    # Retired the same way: code constants now, or Settings -> Business in the database. The
    # three business ones are read once, by load_business, to fill the database on a first start.
    legacy_trust_proxy: str | None = Field(default=None, validation_alias="TRUST_PROXY", repr=False)
    legacy_paypal_env: str | None = Field(default=None, validation_alias="PAYPAL_ENV", repr=False)
    legacy_gemini_model: str | None = Field(default=None, validation_alias="GEMINI_MODEL", repr=False)
    legacy_groq_model: str | None = Field(default=None, validation_alias="GROQ_MODEL", repr=False)
    legacy_ai_timeout: str | None = Field(default=None, validation_alias="AI_TIMEOUT_SECONDS", repr=False)
    legacy_seller_display_name: str | None = Field(default=None, validation_alias="SELLER_DISPLAY_NAME", repr=False)
    legacy_seller_country: str | None = Field(default=None, validation_alias="SELLER_COUNTRY", repr=False)
    legacy_auto_sync_minutes: str | None = Field(default=None, validation_alias="AUTO_SYNC_MINUTES", repr=False)

    @property
    def database_target(self) -> str:
        """What TimelineStore opens: the Postgres URL if set, else the SQLite file."""
        return self.database_url or self.database_path

    @property
    def production(self) -> bool:
        return bool(self.render) or self.app_env.lower() == "production"

    @property
    def paypal_env(self) -> str:
        return PAYPAL_ENV

    @property
    def trust_proxy(self) -> bool:
        """Trust X-Forwarded-For / -Proto / -Host: only behind Render's proxy, so that a direct
        client can't spoof them anywhere else."""
        return bool(self.render)

    @property
    def retired_variables(self) -> list[str]:
        """The names of retired variables that are still set. DATABASE_PATH is retired on Render
        only: locally it still picks the SQLite file."""
        names = [info.validation_alias for name, info in Settings.model_fields.items()
                 if name.startswith("legacy_") and getattr(self, name) is not None]
        if self.render and "database_path" in self.model_fields_set:
            names.append("DATABASE_PATH")
        return names

    @property
    def base_url(self) -> str:
        """The public address for absolute links; "" (links stay relative) when it's unknown."""
        return (self.public_base_url or self.render_external_url or "").rstrip("/")

    def production_problems(self) -> list[str]:
        """What stops the app starting in production. Names settings, never their values."""
        required = {"DATABASE_URL": self.database_url, "APP_SECRET": self.app_secret,
                    "PAYPAL_CLIENT_ID": self.paypal_client_id,
                    "PAYPAL_CLIENT_SECRET": self.paypal_client_secret,
                    "PAYPAL_WEBHOOK_ID": self.paypal_webhook_id}
        problems = [f"{name} is missing" for name, value in required.items() if not value]
        if self.app_secret and len(self.app_secret) < MIN_APP_SECRET:
            problems.append(f"APP_SECRET must be at least {MIN_APP_SECRET} characters")
        if self.allow_unverified_webhooks:
            problems.append("ALLOW_UNVERIFIED_WEBHOOKS must be off")
        return problems

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
