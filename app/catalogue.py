"""What the shop sells. Ids are internal: the AI, buyers and reports only ever see the description."""

# Prices live on the server, never in the browser, so a buyer can't change them.
CATALOGUE = {
    "brand-kit": {
        "description": "Printed brand kit (logo + business cards), shipped from India",
        "amount": "120.00",
        "currency": "USD",
    },
    # Day 7: more printed products, so the demo dashboard has varied amounts. The checkout
    # page still sells the brand kit.
    "poster-set": {"description": "Set of three art prints, shipped from India",
                   "amount": "60.00", "currency": "USD"},
    "logo-refresh": {"description": "Logo refresh with printed style sheet, shipped from India",
                     "amount": "85.00", "currency": "USD"},
    "brand-guide": {"description": "Bound brand guidelines book, shipped from India",
                    "amount": "240.00", "currency": "USD"},
}

ITEM_NAMES = {item_id: item["description"] for item_id, item in CATALOGUE.items()}


def item_name(item_id: str | None, fallback: str = "the order") -> str:
    return ITEM_NAMES.get(item_id or "", fallback)
