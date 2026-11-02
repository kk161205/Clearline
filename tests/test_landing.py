"""The landing page at /, the shop at /shop, and the Sandbox note on what buyers see."""

import re
from pathlib import Path

from app.buyer import buyer_token
from app.store import TimelineStore
from tests.support import make_client

ROOT = Path(__file__).parent.parent
MCP_URL = "https://clearline-cbyb.onrender.com/mcp"
NOTE = "test accounts only, no real money"


def anonymous(tmp_path):
    seller, settings = make_client(tmp_path)  # an account exists, so nothing redirects to /setup
    client, _ = make_client(tmp_path, login=False)
    return client, settings


def test_landing_is_public_and_says_what_it_is(tmp_path):
    client, _ = anonymous(tmp_path)
    res = client.get("/")
    assert res.status_code == 200 and "<title>Clearline" in res.text
    assert NOTE in res.text and "paypal-button" not in res.text


def test_the_three_ways_in(tmp_path):
    html = anonymous(tmp_path)[0].get("/").text
    assert 'href="/shop">Try the shop' in html and "PayPal Sandbox personal account" in html
    assert 'href="/login">Seller login' in html and "Devpost testing instructions" in html
    assert f'data-copy="{MCP_URL}"' in html and f"<code>{MCP_URL}</code>" in html
    assert "github.com/kk161205/Clearline" in html and "docs/SYSTEM.md" in html


def test_landing_has_the_default_policy_and_loads_nothing_third_party(tmp_path):
    res = anonymous(tmp_path)[0].get("/")
    assert res.headers["content-security-policy"].startswith("default-src 'self'; script-src 'self';")
    assert "paypal" not in res.headers["content-security-policy"]
    assert not re.findall(r'<(?:script|link|img|iframe)[^>]+(?:src|href)="https?://', res.text)


def test_no_credentials_anywhere_on_the_public_pages(tmp_path):
    client, _ = anonymous(tmp_path)
    for path in ("/", "/shop", "/login"):
        text = client.get(path).text.lower()
        assert "password:" not in text and "judge@" not in text and "@clearline" not in text, path
    for page in (ROOT / "app/static").glob("*.html"):
        assert "judge@" not in page.read_text(encoding="utf-8"), page.name


def test_the_shop_moved_and_keeps_its_checkout(tmp_path):
    res = anonymous(tmp_path)[0].get("/shop")
    assert res.status_code == 200 and "<paypal-button" in res.text
    assert "paypal.com" in res.headers["content-security-policy"]
    assert NOTE in res.text and 'href="/"' in res.text


def test_the_paid_receipt_stays_on_the_shop():
    script = (ROOT / "app/static/pages/buyer.js").read_text(encoding="utf-8")
    assert "`/shop?receipt=" in script and "`/?receipt=" not in script


def test_the_buyer_page_shows_the_sandbox_note(tmp_path):
    client, settings = anonymous(tmp_path)
    TimelineStore(settings.database_target).add("O1", "payment_captured", {"capture_id": "C1"})
    assert NOTE in client.get(f"/o/{buyer_token(settings.app_secret, 'O1')}").text


def test_the_setting_locks_are_on_the_page_for_demo_accounts():
    html = (ROOT / "app/static/seller.html").read_text(encoding="utf-8")
    assert html.count('class="muted small demo-note" hidden') == 3
