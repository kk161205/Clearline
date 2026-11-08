"""The full live end-to-end run against a deployed Clearline, in PayPal Sandbox only.

    python -m app.e2e [--only checkout,ship,...] [--headed] [--list]
    python -m app.e2e --only invoice-overdue [--invoice-id INV2-...]   (two days after "invoice")

Drives the real pages with Playwright: real Sandbox payments, refunds and disputes, on the real
database, so every run adds orders to the live shop. Never part of CI. Playwright isn't in
requirements.txt: install it somewhere and put that on PYTHONPATH.

Needs E2E_BASE_URL, E2E_SELLER_EMAIL, E2E_SELLER_PASSWORD, E2E_BUYER_EMAIL and E2E_BUYER_PASSWORD,
from the environment or .env. Results go to <out>/<timestamp>/: report.md, and a folder per flow
with a screenshot per step and a video.

Guards: it refuses to start unless /api/config says sandbox, stops any navigation to a PayPal page
that isn't sandbox.paypal.com, restores the auto-policy and the Insights layout after every flow,
and stops a flow on a captcha or 2-step check rather than trying to get around it.
"""

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .logsafe import redact_path

OUT = r"D:\Temp\claude\clearline\e2e"
SANDBOX_HOST = "sandbox.paypal.com"
SANDBOX_SITE = "https://www.sandbox.paypal.com"
CHALLENGE_WORDS = ("captcha", "security challenge", "i'm not a robot", "verify it's you", "confirm it's you",
                   "two-step", "2-step", "enter the code we sent", "unusual activity",
                   "tried too many times")  # a login lockout: retrying only extends it
SUBMIT_BUTTONS = ("#payment-submit-btn", "[data-testid='submit-button-initial']",
                  "button:has-text('Complete Purchase')", "button:has-text('Pay Now')",
                  "button:has-text('Continue to Review Order')", "button:has-text('Review Order')")
COOKIE_BUTTONS = "#acceptAllButton, button:has-text('Yes, I accept')"
PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"
TEST_DAILY_CAP = "5000"  # a run's own refunds count against the cap, and it's restored after each flow


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="E2E_", extra="ignore")

    base_url: str
    seller_email: str = Field(repr=False)
    seller_password: str = Field(repr=False)
    buyer_email: str = Field(repr=False)
    buyer_password: str = Field(repr=False)

    @property
    def base(self) -> str:
        return self.base_url.rstrip("/")

    @property
    def secrets(self) -> list[str]:
        return [self.seller_email, self.seller_password, self.buyer_email, self.buyer_password]


class Refused(Exception):
    """A guard stopped the run or the flow."""


class Blocked(Exception):
    """PayPal showed a captcha, a 2-step check or a block. The flow stops there."""


def assert_sandbox(config: dict) -> None:
    """The site must say sandbox in both places /api/config gives it."""
    if config.get("env") != "sandbox":
        raise Refused(f"/api/config reports env {config.get('env')!r}, not sandbox")
    host = urlsplit(config.get("sdkUrl") or "").hostname or ""
    if not is_sandbox_host(host):
        raise Refused("/api/config's SDK isn't on sandbox.paypal.com")


def is_sandbox_host(host: str) -> bool:
    return host == SANDBOX_HOST or host.endswith("." + SANDBOX_HOST)


def page_allowed(url: str) -> bool:
    """False for a PayPal page that isn't Sandbox. Other sites aren't this guard's concern."""
    host = (urlsplit(url).hostname or "").lower()
    return is_sandbox_host(host) or not (host == "paypal.com" or host.endswith(".paypal.com"))


def scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def looks_like_challenge(url: str, body: str) -> str | None:
    """The reason a PayPal page looks like a human check, or None."""
    if "/challenge" in url or "captcha" in url.lower():
        return "challenge page"
    body = body.lower()
    return next((word for word in CHALLENGE_WORDS if word in body), None)


def wait_for(check, seconds: float, what: str, every: float = 5):
    """check() until it returns something truthy; raises if it never does."""
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what}: not seen within {seconds:.0f} s")
        time.sleep(every)


class Api:
    """The seller's session outside the browser: polling, and restoring settings. It works even if
    a browser context has died, which is what makes the restore dependable."""

    def __init__(self, config: Config):
        self.http = httpx.Client(base_url=config.base, timeout=60)
        res = self.http.post("/api/login", json={"email": config.seller_email, "password": config.seller_password})
        if res.status_code != 200:
            raise Refused(f"Seller login failed: HTTP {res.status_code}")

    def get(self, path: str):
        res = self.http.get(path)
        res.raise_for_status()
        return res.json()

    def kinds(self, order_id: str) -> list[str]:
        res = self.http.get(f"/api/orders/{order_id}/timeline")
        return [e["kind"] for e in res.json()] if res.status_code == 200 else []

    def events(self, order_id: str, kind: str) -> list[dict]:
        res = self.http.get(f"/api/orders/{order_id}/timeline")
        return [e for e in res.json() if e["kind"] == kind] if res.status_code == 200 else []

    def policy(self) -> dict:
        saved = self.get("/api/policy")
        return {k: v for k, v in saved.items() if k not in ("spent_today", "reasons")}

    def layout(self) -> dict | None:
        return self.get("/api/insights/layout").get("state")

    def restore_layout(self, state: dict | None) -> None:
        res = self.http.delete("/api/insights/layout") if state is None else self.http.put("/api/insights/layout", json=state)
        res.raise_for_status()


@dataclass
class Order:
    id: str
    link: str


@dataclass
class Flow:
    name: str
    run: "Run"
    dir: Path
    status: str = PASS
    seconds: float = 0
    detail: str = ""
    notes: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    shots: int = 0
    contexts: dict = field(default_factory=dict)
    pages: dict = field(default_factory=dict)
    active: object = None

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def page(self, who: str):
        """The flow's page for "seller" or "buyer", in its own browser context with video."""
        if who not in self.pages:
            state = self.run.states.get(who)
            context = self.run.browser.new_context(
                viewport={"width": 1280, "height": 900}, record_video_dir=str(self.dir / "video"),
                record_video_size={"width": 1280, "height": 900},
                storage_state=str(state) if state and state.exists() else None)
            self.run.watch(context, self)
            self.contexts[who] = context
            self.pages[who] = context.new_page()
        self.active = self.pages[who]
        return self.pages[who]

    def shot(self, label: str, page=None) -> str:
        self.shots += 1
        path = self.dir / f"{self.shots:02d}-{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')[:50]}.png"
        try:
            (page or self.active).screenshot(path=str(path))
        except Exception as err:  # a closed popup mustn't fail the step it's recording
            self.note(f"No screenshot for {label}: {type(err).__name__}")
        return str(path)

    @contextmanager
    def step(self, label: str):
        print(f"  [{self.name}] {label}", flush=True)
        try:
            yield
        except Exception:
            self.detail = self.detail or label
            self.failure_shot = self.shot(f"failed {label}")
            raise
        self.shot(label)

    failure_shot: str = ""


class Run:
    def __init__(self, config: Config, out: Path, browser, invoice_id: str | None, order_id: str | None):
        self.config, self.out, self.browser = config, out, browser
        self.invoice_id, self.order_id = invoice_id, order_id
        self.api = Api(config)
        self.policy = self.api.policy()
        self.layout = self.api.layout()
        self.flows: list[Flow] = []
        self.states = {"seller": out / ".seller-state.json", "buyer": out / ".buyer-state.json"}
        self.violations: list[str] = []
        self.invoice_file = out.parent / "invoice.json"

    def watch(self, context, flow: Flow) -> None:
        """Every context gets the Sandbox guard and the failed-request log."""
        def paypal(route):
            url = route.request.url
            if page_allowed(url):
                route.continue_()
                return
            if route.request.resource_type == "document":
                self.violations.append(urlsplit(url).hostname or "?")
            route.abort()

        host = urlsplit(self.config.base).hostname

        def response(res):
            if res.status >= 400 and urlsplit(res.url).hostname == host:
                path = redact_path(urlsplit(res.url).path)
                flow.failed_requests.append(f"{res.request.method} {path} {res.status} "
                                            f"X-Request-ID {res.headers.get('x-request-id', 'none')}")

        def console(page):
            def on_message(message):  # PayPal's own pages have their own policy: not ours to judge
                if (message.type == "error" and "Content Security Policy" in message.text
                        and urlsplit(page.url).hostname == host):
                    flow.note(f"CSP violation in the browser: {message.text[:200]}")
            return on_message

        context.route(re.compile(r"^https?://([^/]*\.)?paypal\.com(/|$)"), paypal)
        context.on("response", response)
        context.on("page", lambda page: page.on("console", console(page)))

    def check_guards(self) -> None:
        if self.violations:
            raise Refused(f"Stopped a navigation to a non-Sandbox PayPal page: {self.violations[-1]}")

    def restore(self, flow: Flow | None = None) -> None:
        """Puts back the auto-policy and the Insights layout, whatever happened."""
        try:
            if self.api.policy() != self.policy:
                self.api.http.put("/api/policy", json=self.policy).raise_for_status()
                if flow:
                    flow.note("Auto-policy restored.")
            if self.api.layout() != self.layout:
                self.api.restore_layout(self.layout)
                if flow:
                    flow.note("Insights layout restored.")
        except Exception as err:
            message = f"RESTORE FAILED, check Settings by hand: {type(err).__name__}"
            print(message, flush=True)
            if flow:
                flow.note(message)

    # ---- Shared steps ----------------------------------------------------------------------

    def challenge_check(self, page, flow: Flow) -> None:
        self.check_guards()
        if not is_sandbox_host((urlsplit(page.url).hostname or "")):
            return
        try:
            body = page.inner_text("body", timeout=3000)
        except Exception:
            return
        if reason := looks_like_challenge(page.url, body):
            shot = flow.shot("paypal challenge", page)
            flow.failure_shot = shot
            raise Blocked(f"PayPal showed: {reason}")

    def visible(self, page, selector: str) -> bool:
        try:
            return page.locator(selector).first.is_visible(timeout=300)
        except Exception:
            return False

    def paypal_login(self, page, flow: Flow) -> bool:
        """Fills in whichever login step is showing. True if it did anything."""
        acted = False
        if self.visible(page, "#email") and not page.locator("#email").input_value():
            page.fill("#email", self.config.buyer_email)
            acted = True
        if self.visible(page, "#btnNext"):
            page.click("#btnNext")
            page.wait_for_timeout(2000)
            acted = True
        if self.visible(page, "#password") and not page.locator("#password").input_value():
            page.fill("#password", self.config.buyer_password)
            acted = True
        if self.visible(page, "#btnLogin"):
            page.click("#btnLogin")
            page.wait_for_timeout(3000)
            acted = True
        return acted

    def approve_popup(self, popup, flow: Flow) -> None:
        """Logs in as the Sandbox buyer and approves, until PayPal closes the popup."""
        deadline, shots = time.monotonic() + 120, 0
        while not popup.is_closed():
            if time.monotonic() > deadline:
                flow.shot("popup stuck", popup)
                raise AssertionError("The PayPal popup didn't finish within 2 minutes")
            try:
                if not is_sandbox_host(urlsplit(popup.url).hostname or "") and popup.url != "about:blank":
                    raise Refused(f"The popup left Sandbox: {urlsplit(popup.url).hostname}")
                self.challenge_check(popup, flow)
                if self.visible(popup, COOKIE_BUTTONS):
                    popup.locator(COOKIE_BUTTONS).first.click()
                self.paypal_login(popup, flow)
                for selector in SUBMIT_BUTTONS:
                    if self.visible(popup, selector):
                        if shots < 3:
                            shots += 1
                            flow.shot("paypal review", popup)
                        popup.locator(selector).first.click(timeout=5000)
                        break
                popup.wait_for_timeout(1500)
            except Exception as err:
                if popup.is_closed():
                    return
                if isinstance(err, (Blocked, Refused, AssertionError)):
                    raise

    def save_state(self, flow: Flow, who: str) -> None:
        """Later flows reuse this login, so PayPal sees fewer sign-ins. Deleted after the run."""
        if who in flow.contexts:
            flow.contexts[who].storage_state(path=str(self.states[who]))

    def buy(self, flow: Flow) -> Order:
        """Checkout at /shop through the PayPal popup. Returns the order, once captured."""
        page = flow.page("buyer")
        created = {}

        def on_response(res):
            if res.request.method == "POST" and res.url.endswith("/api/orders") and res.status == 200:
                created["id"] = res.json().get("orderId")

        page.on("response", on_response)
        with flow.step("open checkout"):
            page.goto(self.config.base + "/shop", wait_until="load")
            page.locator("paypal-button").wait_for(state="visible", timeout=60000)
        with flow.step("pay through the PayPal popup"):
            with page.expect_popup(timeout=30000) as opening:
                page.locator("paypal-button").click()
            self.approve_popup(opening.value, flow)
            page.locator("#paid").wait_for(state="visible", timeout=60000)
        self.check_guards()
        link = page.locator("#paid-help").get_attribute("href")
        self.save_state(flow, "buyer")
        assert created.get("id"), "No order id came back from /api/orders"
        return Order(created["id"], link if link.startswith("http") else self.config.base + link)

    def seller(self, flow: Flow):
        """The seller's page, logged in."""
        page = flow.page("seller")
        page.goto(self.config.base + "/seller", wait_until="load")
        if "/login" in page.url:
            page.fill("input[type=email], input[name=email]", self.config.seller_email)
            page.fill("input[type=password]", self.config.seller_password)
            page.click("button[type=submit]")
            page.wait_for_url("**/seller")
            self.save_state(flow, "seller")
        return page

    def open_order(self, flow: Flow, order: Order):
        """/seller with this order's card open; returns the card."""
        page = self.seller(flow)
        card = page.locator(f'article.order[data-order-id="{order.id}"]')
        card.wait_for(state="visible", timeout=60000)
        head = card.locator(".order-head")
        if head.get_attribute("aria-expanded") != "true":
            head.click()
        return card

    def ship(self, flow: Flow, order: Order, number: str = "1234567890") -> None:
        card = self.open_order(flow, order)
        form = card.locator("form.ship")
        form.locator("[name=tracking_number]").fill(number)
        form.locator("[name=carrier]").select_option("DHL")
        form.locator("button").click()
        wait_for(lambda: "tracking_added" in self.api.kinds(order.id), 30, "tracking on the timeline", 2)

    def ask_refund(self, flow: Flow, order: Order, reason: str = "changed_mind") -> None:
        """On the buyer link, as the buyer sees it."""
        page = flow.page("buyer")
        page.goto(order.link, wait_until="load")
        page.locator('[data-open="refund"]').click()
        page.locator("#reason").select_option(reason)
        page.locator('[data-send="refund"]').click()
        wait_for(lambda: "buyer_request" in self.api.kinds(order.id), 30, "the buyer's request", 2)

    def policy_off(self, flow: Flow) -> None:
        """For a flow that needs the seller to decide. run.restore() puts it back."""
        if self.api.policy()["enabled"]:
            self.api.http.put("/api/policy", json={**self.api.policy(), "enabled": False}).raise_for_status()
            flow.note("Auto-policy was on: turned off for this flow.")

    def set_policy(self, flow: Flow, *, per_order: str, per_day: str, only_unshipped: bool, reasons: list[str]) -> None:
        """Turns the auto-policy on through Settings. run.restore() puts it back."""
        page = self.seller(flow)
        page.evaluate("document.getElementById('policy').open = true")
        form = page.locator("#policy-form")
        form.locator('[data-pol="enabled"]').check()
        form.locator('[data-pol="currency"]').select_option("USD")
        form.locator('[data-pol="max_refund_per_order"]').fill(per_order)
        form.locator('[data-pol="daily_cap"]').fill(per_day)
        form.locator('[data-pol="only_if_not_shipped"]').set_checked(only_unshipped)
        for box in form.locator("[data-pol-reason]").all():
            box.set_checked(box.get_attribute("data-pol-reason") in reasons)
        page.click("#policy-save")
        page.locator("[data-ok]").click()
        wait_for(lambda: self.api.policy()["enabled"], 30, "the policy being on", 2)


# ---- The flows -------------------------------------------------------------------------------

FLOWS = {}


def flow(name: str, default: bool = True):
    def register(fn):
        fn.default = default
        FLOWS[name] = fn
        return fn
    return register


def system_status_text(run: Run, flow: Flow, page) -> str:
    page.evaluate("document.getElementById('system').open = true")
    page.click("#system-run")
    page.wait_for_timeout(3000)
    return page.inner_text("#system-checks")


@flow("checkout")
def checkout(run: Run, f: Flow) -> None:
    started = time.time()
    order = run.buy(f)
    f.detail = f"order …{order.id[-6:]}"
    with f.step("the order shows on /seller"):
        run.open_order(f, order)
    with f.step("Last webhook shows PAYMENT.CAPTURE.COMPLETED"):
        page = f.page("seller")

        def webhook():
            seen = run.api.get("/api/system/status")["last_webhook"]
            return seen and seen["event_type"] == "PAYMENT.CAPTURE.COMPLETED" and seen["at"] >= started

        wait_for(webhook, 180, "PAYMENT.CAPTURE.COMPLETED as the last webhook", 10)
        assert "PAYMENT.CAPTURE.COMPLETED" in system_status_text(run, f, page), "Not on the System status panel"
    with f.step("Log out ends the session"):
        page = run.seller(f)
        page.locator(".shell-account [data-logout]").click()
        page.locator("#note").wait_for(state="visible", timeout=30000)  # "You're logged out" on /login
        page.goto(run.config.base + "/seller", wait_until="load")
        assert "/login" in page.url, "/seller still open after logging out"
        run.states["seller"].unlink(missing_ok=True)  # that login is gone: the next flow signs in again
    f.detail = ""


@flow("ship")
def ship(run: Run, f: Flow) -> None:
    order = run.buy(f)
    with f.step("mark shipped with DHL"):
        run.ship(f, order)
    with f.step("the timeline shows the shipment"):
        tracked = run.api.events(order.id, "tracking_added")[-1]["payload"]
        assert (tracked["carrier"], tracked["tracking_number"]) == ("DHL", "1234567890"), tracked
        f.page("seller").reload(wait_until="load")
    with f.step("the buyer link shows shipped"):
        page = f.page("buyer")
        page.goto(order.link, wait_until="load")
        page.get_by_text("Shipped with").first.wait_for(state="visible", timeout=30000)


@flow("refund")
def refund(run: Run, f: Flow) -> None:
    run.policy_off(f)  # on, it would refund this before the seller could
    order = run.buy(f)
    with f.step("the buyer asks for a refund: changed my mind"):
        run.ask_refund(f, order)
    with f.step("refund in full from /seller"):
        card = run.open_order(f, order)
        card.locator('[data-refund="full"]').click()
        f.page("seller").locator("[data-ok]").click()
        wait_for(lambda: "refund_sent" in run.api.kinds(order.id), 60, "the refund on the timeline", 3)
    with f.step("the refund webhook lands"):
        wait_for(lambda: "webhook_payment_capture_refunded" in run.api.kinds(order.id), 180, "PAYMENT.CAPTURE.REFUNDED", 10)
    with f.step("the money state shows refunded"):
        page = f.page("buyer")
        page.goto(order.link, wait_until="load")
        page.get_by_text("Refunded in full").first.wait_for(state="visible", timeout=30000)
        rows = run.api.get("/api/dashboard")["rows"]
        row = next(r for r in rows if r["ref"] == f"…{order.id[-6:]}")
        assert float(row["refunded"]) == float(row["amount"]), row
        f.page("seller").goto(run.config.base + "/dashboard", wait_until="load")


def frame_with(page, selector: str):
    """The frame showing `selector`, or None. PayPal's "Report a Problem" sheet is an iframe."""
    for frame in page.frames:
        try:
            if frame.locator(selector).first.is_visible(timeout=300):
                return frame
        except Exception:
            continue
    return None


def pick_radio(page, value: str) -> bool:
    """Chooses a radio through its label (PayPal's real inputs sit under a styled label)."""
    radio = page.locator(f"input[type=radio][value={value}]")
    if not radio.count():
        return False
    if not radio.first.is_checked():
        page.locator(f'label[for="{radio.first.get_attribute("id")}"]').first.click()
        page.wait_for_timeout(800)
    return True


def fill_filing_screen(page) -> None:
    """Whatever this step of PayPal's filing wizard is asking, answered for item-not-received."""
    if page.locator("#dropdownMenuSelect_Itemtype").count():
        page.select_option("#dropdownMenuSelect_Itemtype", value="PRODUCT")
        page.wait_for_timeout(1000)
        page.locator("#dropdownMenuSelect_Category").select_option(index=2)
        page.wait_for_timeout(1000)
    pick_radio(page, "NOT_DELIVERED")
    pick_radio(page, "NO")  # "Have you contacted the seller?" is optional
    amount = page.locator('input[id*="expected_refund"], input[name*="expected_refund"], '
                          'input[data-testid*="expected_refund"]').first
    if amount.count() and amount.is_visible() and not amount.input_value():
        amount.focus()  # a masked field with a label on top: fill() doesn't reach it
        page.keyboard.type("120.00", delay=60)
        page.wait_for_timeout(600)
    details = page.locator("textarea:visible")
    if details.count() and not details.first.input_value():
        details.first.fill("My order still hasn't arrived and the tracking hasn't moved.")
        page.wait_for_timeout(600)


def open_dispute_on_paypal(run: Run, f: Flow) -> str:
    """As the buyer: the newest payment, reported as not received. Returns PayPal's case id."""
    page = f.page("buyer")
    page.goto(SANDBOX_SITE + "/disputes/dashboard/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)
    run.challenge_check(page, f)
    if run.paypal_login(page, f):
        page.wait_for_timeout(8000)
        run.challenge_check(page, f)
    if run.visible(page, COOKIE_BUTTONS):
        page.locator(COOKIE_BUTTONS).first.click()
    # A saved draft is listed first ("Your info will be saved..."): never the order we just bought.
    cards = page.locator("[data-testid=transaction-card]").filter(has_text="Payment")
    fresh = [card for card in cards.all() if "your info" not in card.inner_text().lower()]
    assert fresh, "No payment to report on PayPal's disputes page"
    fresh[0].click()
    page.wait_for_timeout(4000)
    sheet = wait_for(lambda: frame_with(page, "text=Report a Problem"), 45, "PayPal's Report a Problem sheet", 2)
    sheet.get_by_text("Report a Problem", exact=False).first.click()
    def reasons():
        # A shipped order first gets PayPal's own tracking info and a button to go on anyway.
        found = frame_with(page, "[data-testid=elg-item-0]")
        if not found and (past := frame_with(page, "button:has-text('Report Issue to PayPal')")):
            past.get_by_role("button", name="Report Issue to PayPal").click()
        return found

    reason = wait_for(reasons, 60, "PayPal's list of reasons", 2)
    reason.locator("[data-testid=elg-item-0]").click()
    page.wait_for_timeout(5000)
    forward = re.compile(r"^\s*(Continue|Next|Submit|Submit Case|File (a )?(Case|Dispute)|Open (a )?(Case|Dispute))\s*$", re.I)
    last, repeats = "", 0
    for _ in range(25):
        run.challenge_check(page, f)
        text = page.inner_text("body")
        found = re.search(r"Case ID:\s*(PP-[A-Z0-9-]+)", text)
        if found:
            return found.group(1)
        fill_filing_screen(page)
        repeats = repeats + 1 if text == last else 0
        last = text
        assert repeats < 3, "PayPal's filing wizard stopped moving"
        f.shot("paypal filing step", page)
        button = page.locator("button:visible").filter(has_text=forward)
        if button.count():
            button.first.click()
        page.wait_for_timeout(5000)
    raise AssertionError("PayPal's filing wizard never finished")


@flow("dispute")
def dispute(run: Run, f: Flow) -> None:
    order = run.buy(f)
    with f.step("open an item-not-received dispute at PayPal"):
        f.note(f"PayPal case: {open_dispute_on_paypal(run, f)}")
    with f.step("the dispute reaches /seller"):
        page = run.seller(f)

        def arrived():
            if "dispute_created" in run.api.kinds(order.id):
                return True
            page.click("#sync")
            return False

        wait_for(arrived, 600, "the dispute on the order", 30)
    automatic = True
    try:
        with f.step("the AI recommendation appears"):
            wait_for(lambda: ai_answered(run, order), 180, "an AI recommendation", 10)
    except AssertionError as err:
        automatic = False
        f.note(f"BUG: {err}. Finishing the flow with Ask AI again, so the rest is still tested.")
    respond_to_dispute(run, f, order)
    assert automatic, "The AI recommendation never appeared by itself"


def ai_answered(run: Run, order: Order) -> bool:
    kinds = run.api.kinds(order.id)
    return "ai_recommendation" in kinds or "ai_failed" in kinds


def respond_to_dispute(run: Run, f: Flow, order: Order) -> None:
    """Shows the recommendation, asking for one if there's none, and sends it if it's in scope."""
    card = run.open_order(f, order)
    if not ai_answered(run, order):
        with f.step("ask the AI by hand"):
            card.locator("[data-regen]").click()
            wait_for(lambda: ai_answered(run, order), 120, "an AI recommendation after Ask AI again", 5)
    with f.step("the recommendation shows on the order"):
        assert "ai_recommendation" in run.api.kinds(order.id), "The AI failed on this dispute"
        card = run.open_order(f, order)
        card.locator(".ai").first.wait_for(state="visible", timeout=30000)
    with f.step("send the recommended response if it's a message or evidence"):
        recommendation = run.api.events(order.id, "ai_recommendation")[-1]["payload"]["recommendation"]
        states = run.api.events(order.id, "dispute_updated") or run.api.events(order.id, "dispute_created")
        stage, decision = states[-1]["payload"].get("dispute_life_cycle_stage"), recommendation["decision"]
        f.note(f"Dispute stage {stage}; the AI recommended {decision}.")
        button = card.locator("[data-approve]")
        if decision != "FIGHT" or stage != "INQUIRY" or not button.count():
            f.note(f"Nothing sent: only an inquiry-stage message or evidence step is in scope "
                   f"(approve button shown: {bool(button.count())}).")
            return
        page = f.page("seller")
        with page.expect_response(lambda r: r.url.endswith("/act")) as answer:
            button.first.click()
            page.locator("[data-ok]").click()
        reply = answer.value
        f.note(f"PayPal's answer through /act: HTTP {reply.status}: {reply.text()[:400]}")


def recommendation_note(run: Run, order: Order) -> dict:
    """The automatic recommendation, in words for the report: decision, confidence, reasons, provider."""
    event = run.api.events(order.id, "ai_recommendation")[-1]["payload"]
    rec = event["recommendation"]
    return {"decision": rec["decision"], "confidence": rec["confidence"], "provider": event.get("provider"),
            "model": event.get("model"), "trigger": event.get("trigger"),
            "reasons": [r["text"] for r in rec.get("reasons", [])],
            "message_to_buyer": rec.get("message_to_buyer"), "note_to_paypal": rec.get("note_to_paypal")}


def shipped_dispute(run: Run, f: Flow) -> Order:
    """A shipped order with an item-not-received dispute and its automatic recommendation."""
    order = run.buy(f)
    with f.step("mark it shipped with DHL"):
        run.ship(f, order)
    with f.step("open an item-not-received dispute at PayPal"):
        f.note(f"PayPal case: {open_dispute_on_paypal(run, f)}")
    with f.step("the dispute reaches /seller"):
        page = run.seller(f)

        def arrived():
            if "dispute_created" in run.api.kinds(order.id):
                return True
            page.click("#sync")
            return False

        wait_for(arrived, 600, "the dispute on the order", 30)
    with f.step("the recommendation appears by itself"):
        wait_for(lambda: ai_answered(run, order), 180, "an AI recommendation", 10)
        assert "ai_recommendation" in run.api.kinds(order.id), "The AI failed on this dispute"
        seen = recommendation_note(run, order)
        assert seen["trigger"] == "auto", seen["trigger"]
        f.note("Recommendation: " + json.dumps(seen))
    return order


@flow("dispute-shipped", default=False)
def dispute_shipped(run: Run, f: Flow) -> None:
    order = shipped_dispute(run, f)
    respond_to_dispute(run, f, order)


@flow("dispute-respond", default=False)
def dispute_respond(run: Run, f: Flow) -> None:
    """The second half of "dispute", on an open dispute: --order-id."""
    assert run.order_id, "Pass --order-id"
    respond_to_dispute(run, f, Order(run.order_id, ""))


@flow("policy-negative")
def policy_negative(run: Run, f: Flow) -> None:
    with f.step("turn the auto-policy on, for changed my mind"):
        run.set_policy(f, per_order="150", per_day=TEST_DAILY_CAP, only_unshipped=True, reasons=["changed_mind"])
    order = run.buy(f)
    with f.step("mark it shipped"):
        run.ship(f, order)
    with f.step("the buyer asks for a refund"):
        run.ask_refund(f, order)
    with f.step("it goes to the seller, with no refund"):
        wait_for(lambda: "auto_policy_skipped" in run.api.kinds(order.id), 30, "the policy declining", 2)
        kinds = run.api.kinds(order.id)
        assert "refund_sent" not in kinds, "It auto-refunded a shipped order"
        reason = run.api.events(order.id, "auto_policy_skipped")[-1]["payload"]["reason"]
        f.note(f"The policy declined: {reason}")
        card = run.open_order(f, order)
        card.locator('[data-refund="full"]').wait_for(state="visible", timeout=30000)


def mcp_calls(url: str, link: str) -> tuple[dict, dict]:
    """get_order_status, then request_refund, with the official MCP client."""
    from mcp.client.client import Client

    def body(result) -> dict:
        if result.structured_content:
            return result.structured_content
        return json.loads(result.content[0].text)

    async def calls():
        async with Client(url) as client:
            status = await client.call_tool("get_order_status", {"order_link": link})
            refund = await client.call_tool("request_refund", {"order_link": link, "reason": "changed_mind",
                                                               "message": "Ordered the wrong size by mistake, sorry."})
            return body(status), body(refund)

    with ThreadPoolExecutor(max_workers=1) as pool:  # Playwright's sync API owns this thread's loop
        return pool.submit(lambda: asyncio.run(calls())).result(timeout=120)


@flow("agent")
def agent(run: Run, f: Flow) -> None:
    order = run.buy(f)
    with f.step("the policy allows it for a moment"):
        run.set_policy(f, per_order="150", per_day=TEST_DAILY_CAP, only_unshipped=True, reasons=["changed_mind"])
    with f.step("MCP: get_order_status, then request_refund"):
        status, result = mcp_calls(run.config.base + "/mcp", order.link)
        f.note(f"get_order_status: amount {status.get('amount')} {status.get('currency')}; "
               f"request_refund: status {result.get('status')}")
        assert str(result.get("status")).startswith("refunded"), f"Not refunded at once: {result.get('status')}"
    with f.step("the instant refund is on the timeline"):
        sent = run.api.events(order.id, "refund_sent")[-1]["payload"]
        assert sent["trigger"] != "seller", sent
        run.open_order(f, order)


@flow("invoice")
def invoice(run: Run, f: Flow) -> None:
    due = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    page = run.seller(f)
    with f.step("create a draft invoice due tomorrow"):
        page.evaluate("document.getElementById('new-invoice').open = true")
        page.fill("#inv-client", "Kapoor Interiors (Meera Kapoor)")
        page.fill("#inv-email", run.config.buyer_email)
        page.select_option("#inv-currency", "USD")
        page.fill("#inv-due", due)
        page.locator('[data-item="name"]').first.fill("Printed brand kit (logo + business cards)")
        page.locator('[data-item="unit_amount"]').first.fill("25.00")
        with page.expect_response(lambda r: r.url.endswith("/api/invoices") and r.request.method == "POST") as created:
            page.click("#invoice-create")
        assert created.value.status == 200, f"Create answered HTTP {created.value.status}"
        invoice_id = created.value.json()["invoice_id"]
    with f.step("send it"):
        button = page.locator(f'[data-invoice-send="{invoice_id}"]')
        button.click()
        page.locator("[data-ok]").click()
        wait_for(lambda: "invoice_send_sent" in run.api.kinds(invoice_id), 60, "the send on the timeline", 3)
    run.invoice_file.write_text(json.dumps({"invoice_id": invoice_id, "due_date": due}), encoding="utf-8")
    f.note(f"Invoice for part 2: {invoice_id}, due {due}. Run --only invoice-overdue after that date.")
    print(f"Invoice id: {invoice_id}, due {due}", flush=True)


@flow("invoice-overdue", default=False)
def invoice_overdue(run: Run, f: Flow) -> None:
    saved = json.loads(run.invoice_file.read_text(encoding="utf-8")) if run.invoice_file.exists() else {}
    invoice_id = run.invoice_id or saved.get("invoice_id")
    assert invoice_id, "No invoice id: pass --invoice-id, or run --only invoice first"
    row = next((r for r in run.api.get("/api/invoices") if r["invoice_id"] == invoice_id), None)
    assert row, "That invoice isn't on the list"
    assert row["days_overdue"], f"Not overdue yet: {row['display_status']}, due {row['due_date']}"
    page = run.seller(f)
    with f.step("the overdue item shows on Today"):
        page.locator("#attention").get_by_text("overdue").first.wait_for(state="visible", timeout=30000)
    with f.step("draft the reminder"):
        with page.expect_response(lambda r: r.url.endswith("/reminder-draft")) as drafted:
            page.locator(f'[data-invoice-remind="{invoice_id}"]').click()
        cell = page.locator(f'[data-reminder="{invoice_id}"]')
        cell.locator("[data-rem-send]").wait_for(state="visible", timeout=60000)
        draft = drafted.value.json()
        f.note(f"Draft ({draft.get('source')}, {draft.get('provider') or 'no AI'}, {draft.get('days_overdue')} days overdue): "
               f"subject {draft.get('subject')!r}; note {draft.get('note')!r}")
        harsh = re.findall(r"\b(fees?|penalt\w*|interest|legal|lawyer|collections?|court|late charge)\b",
                           f"{draft.get('subject')} {draft.get('note')}", re.I)
        assert not harsh, f"The draft threatens or charges: {harsh}"
    with f.step("send the reminder"):
        with page.expect_response(lambda r: r.url.endswith("/remind")) as answered:
            cell.locator("[data-rem-send]").click()
            page.locator("[data-ok]").click()
        reply = answered.value
        f.note(f"PayPal's answer through /remind: HTTP {reply.status}: {reply.text()[:300]}")
        assert reply.status == 200, f"The reminder wasn't sent: HTTP {reply.status}"
        wait_for(lambda: "invoice_reminder_sent" in run.api.kinds(invoice_id), 60, "the reminder on the timeline", 3)


def ask_insights(f: Flow, page, question: str) -> tuple[str, int, int, float]:
    """A new chat, one question. Waits until the assistant stops thinking. Returns the chat's
    text, the AI turns used, how many failed, and the seconds it took."""
    turns = []
    watcher = lambda res: turns.append(res.status) if res.url.endswith("/api/insights/ai") else None
    page.on("response", watcher)
    page.locator(".ag-studio-ai-thread-new-button").click()
    page.locator("textarea.ag-studio-ai-assistant-textarea").fill(question)
    started = time.monotonic()
    page.locator(".ag-studio-ai-assistant-send-button").click()
    stop = page.get_by_role("button", name="Stop")  # shows only while the assistant is working
    stop.first.wait_for(state="visible", timeout=30000)
    wait_for(lambda: not stop.first.is_visible(), 240, "the assistant finishing", 2)
    seconds = time.monotonic() - started
    page.wait_for_timeout(1500)
    page.remove_listener("response", watcher)
    panel = page.locator(".ag-studio-panel", has_text="AI Assistant").first
    return panel.inner_text(), len(turns), sum(status >= 400 for status in turns), seconds


@flow("insights")
def insights(run: Run, f: Flow) -> None:
    page = run.seller(f)
    with f.step("open Insights and the assistant"):
        page.goto(run.config.base + "/insights", wait_until="load")
        page.locator("canvas").first.wait_for(state="visible", timeout=60000)  # Studio has loaded
        page.get_by_role("button", name="Edit layout").click(timeout=60000)
        page.get_by_role("button", name="Done editing").wait_for(state="visible", timeout=30000)
        page.wait_for_timeout(2000)
        box = page.locator("textarea.ag-studio-ai-assistant-textarea")
        if not box.is_visible():
            page.locator(".ag-studio-panel-header", has_text="AI Assistant").click()
        box.wait_for(state="visible", timeout=30000)
    with f.step("ask which orders need me this week"):
        text, turns, failed, seconds = ask_insights(f, page, "Which orders need me this week?")
        f.note(f"First answer: {turns} AI turns, {failed} failed, {seconds:.0f} s.")
        assert "couldn't answer" not in text.lower(), "The assistant couldn't answer"
        assert turns and not failed, f"{failed} of {turns} AI turns failed"
    with f.step("ask for a bar chart, in a new chat"):
        before = page.locator("canvas").count()
        text, turns, failed, seconds = ask_insights(f, page, "Add a bar chart of refunded amount by buyer country")
        f.note(f"Second answer: {turns} AI turns, {failed} failed, {seconds:.0f} s; "
               f"charts on the page {before} -> {page.locator('canvas').count()}.")
        assert not re.search(r"\b\d+ issues?\b", text), "An advisory note is showing in the chat"
        assert "couldn't answer" not in text.lower(), "The assistant couldn't answer"
        assert turns and not failed, f"{failed} of {turns} AI turns failed"
        page.wait_for_timeout(3000)
        assert page.locator("canvas").count() > before, "No new chart appeared"


# ---- Running them ----------------------------------------------------------------------------

def run_flow(run: Run, name: str, fn=None) -> Flow:
    """Runs one flow (by name, or `fn` for the demo story) and records how it went."""
    directory = run.out / name
    directory.mkdir(parents=True, exist_ok=True)
    f = Flow(name, run, directory)
    run.flows.append(f)
    started = time.monotonic()
    print(f"== {name}", flush=True)
    try:
        (fn or FLOWS[name])(run, f)
    except Blocked as err:
        f.status, f.detail = BLOCKED, str(err)
    except Exception as err:
        f.status = FAIL
        f.detail = f"{f.detail}: {type(err).__name__}: {str(err)[:300]}".strip(": ")
    finally:
        run.restore(f)
        for context in f.contexts.values():
            try:
                context.close()
            except Exception:
                pass
        f.videos = [str(p) for p in (directory / "video").glob("*.webm")]
        f.seconds = time.monotonic() - started
        f.detail = scrub(f.detail, run.config.secrets)
        print(f"== {name}: {f.status} in {f.seconds:.0f} s {f.detail}", flush=True)
    return f


def pre_demo_check(config: Config) -> tuple[int, str]:
    done = subprocess.run([sys.executable, "-m", "app.check", "--base", config.base, "--login-email",
                           config.seller_email, "--password-stdin"], input=config.seller_password + "\n",
                          capture_output=True, text=True, encoding="utf-8", timeout=300)
    return done.returncode, done.stdout


def report(run: Run, check: tuple[int, str] | None) -> str:
    lines = [f"# Clearline live end-to-end run, {run.out.name}", "", f"Site: {run.config.base}", "",
             "| Flow | Result | Time | Detail |", "|---|---|---|---|"]
    for f in run.flows:
        lines.append(f"| {f.name} | {f.status} | {f.seconds:.0f} s | {f.detail.replace('|', '/')} |")
    for f in run.flows:
        lines += ["", f"## {f.name}", "", f"Folder: `{f.dir}`"]
        lines += [f"Video: `{v}`" for v in f.videos]
        if f.failure_shot:
            lines.append(f"Failure screenshot: `{f.failure_shot}`")
        if f.failed_requests:
            lines += ["", "Requests that failed:", *[f"- {r}" for r in f.failed_requests]]
        if f.notes:
            lines += ["", "Notes:", *[f"- {n}" for n in f.notes]]
    if check:
        lines += ["", "## app.check with the seller login", "", "```", check[1].rstrip(), "```"]
    return scrub("\n".join(lines) + "\n", run.config.secrets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.e2e", description="Live end-to-end run, Sandbox only.")
    parser.add_argument("--only", help=f"comma-separated: {', '.join(FLOWS)}")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--list", action="store_true", help="list the flows and stop")
    parser.add_argument("--invoice-id", help="for invoice-overdue")
    parser.add_argument("--order-id", help="for dispute-respond")
    parser.add_argument("--out", default=OUT, help="results folder")
    parser.add_argument("--no-check", action="store_true", help="skip app.check at the end")
    args = parser.parse_args(argv)
    if args.list:
        print("\n".join(FLOWS))
        return 0
    names = args.only.split(",") if args.only else [n for n, fn in FLOWS.items() if fn.default]
    if unknown := set(names) - set(FLOWS):
        parser.error(f"unknown flow: {', '.join(sorted(unknown))}")

    config = Config()
    assert_sandbox(httpx.get(config.base + "/api/config", timeout=120).json())
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright isn't installed: put it on PYTHONPATH (see this file's docstring).")

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=not args.headed)
        run = Run(config, out, browser, args.invoice_id, args.order_id)
        try:
            for name in names:
                run_flow(run, name)
                (out / "report.md").write_text(report(run, None), encoding="utf-8")
        finally:
            run.restore()
            browser.close()
            for state in run.states.values():  # these hold login cookies
                state.unlink(missing_ok=True)
    check = None if args.no_check else pre_demo_check(config)
    (out / "report.md").write_text(report(run, check), encoding="utf-8")
    bad = [f for f in run.flows if f.status != PASS]
    print(f"\nReport: {out / 'report.md'}\n{len(run.flows) - len(bad)} of {len(run.flows)} flows passed.")
    return 1 if bad or (check and check[0]) else 0


if __name__ == "__main__":
    sys.exit(main())
