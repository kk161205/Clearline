"""The shop's own settings: edited on Settings -> Business, stored in the database. They used to
be environment variables; the first start copies any that are still set, so nothing changes."""

import logging

from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import Settings
from .store import TimelineStore

log = logging.getLogger("clearline.business")

BUSINESS_KEY = "business"


class Business(BaseModel):
    # Shown to buyers on their order page ("what is this charge?").
    seller_name: str = Field(default="Demo Design Studio", min_length=1, max_length=120)
    # ISO country code. "IN" shows the India export paperwork checklist.
    seller_country: str = Field(default="IN", pattern=r"^[A-Z]{2}$")
    # Pull disputes from PayPal every N minutes as a safety net for missed webhooks; 0 = off.
    auto_sync_minutes: float = Field(default=3, ge=0, le=1440)

    @field_validator("seller_name", mode="before")
    @classmethod
    def trimmed(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("seller_country", mode="before")
    @classmethod
    def upper(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


def load_business(store: TimelineStore, settings: Settings) -> Business:
    """The saved settings. With none saved yet, the retired variables' values, if any are still
    set (saved once, so later changes to the environment do nothing), else the defaults."""
    saved = store.get_setting(BUSINESS_KEY)
    if saved:
        return Business.model_validate(saved)
    retired = {"seller_name": settings.legacy_seller_display_name,
               "seller_country": settings.legacy_seller_country,
               "auto_sync_minutes": settings.legacy_auto_sync_minutes}
    retired = {name: value for name, value in retired.items() if value not in (None, "")}
    if not retired:
        return Business()
    try:
        business = Business.model_validate(retired)
    except ValidationError as err:
        fields = ", ".join(str(e["loc"][0]) for e in err.errors())
        log.warning("Not copying the old business settings: invalid %s; using the defaults", fields)
        return Business()
    store.set_setting(BUSINESS_KEY, business.model_dump())
    log.info("Copied the old business settings into the database: %s", ", ".join(retired))
    return business
