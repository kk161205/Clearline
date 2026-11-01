"""Response headers for every page and API answer, and the catch-all for unexpected errors.

CSP is one policy per page type. All pages load their scripts from /static, so no policy needs
'unsafe-inline' or 'unsafe-eval' for scripts. style-src keeps 'unsafe-inline': the pages use
style attributes, and AG Grid and PayPal's SDK inject style elements.

What PayPal JS SDK v6 loads on the checkout page, measured with Playwright against Sandbox
(page load, button render and the click that opens the popup; the popup itself is PayPal's own
page, under PayPal's own policy). Live uses https://www.paypal.com in place of the Sandbox host.
  https://www.sandbox.paypal.com   the SDK's scripts, its fetch and beacon calls, a pixel iframe
  https://www.paypalobjects.com    the font of the PayPal button
Nothing else on the page is third-party, and the buyer order page (/o/...) loads no PayPal code
at all, so it has the default policy. /insights adds https://cdn.jsdelivr.net for AG Studio.
AG Grid builds its icon fonts into its script as data: URIs, so the two grid pages allow them.
"""

import json
import logging
from urllib.parse import urlsplit

from .logsafe import redact_path, where

log = logging.getLogger("clearline.security")

# Proxies between the client and the app, each appending one X-Forwarded-For hop: Render's.
TRUSTED_PROXY_HOPS = 1

HSTS = "max-age=31536000"
PERMISSIONS = "camera=(), microphone=(), geolocation=()"
PAYPAL_ASSETS = "https://www.paypalobjects.com"
AG_STUDIO_CDN = "https://cdn.jsdelivr.net"
# FastAPI's own API pages, which only exist outside production. They load Swagger from a CDN.
API_DOCS = ("/docs", "/redoc")


def policy(*, script: str = "", connect: str = "", frame: str = "", img: str = "", font: str = "") -> str:
    """'self' for everything, plus the extra sources one page type needs."""
    directives = {"default-src": "'self'", "script-src": f"'self' {script}",
                  "style-src": "'self' 'unsafe-inline'", "img-src": f"'self' data: {img}",
                  "font-src": f"'self' {font}", "connect-src": f"'self' {connect}", "frame-src": f"'self' {frame}",
                  "object-src": "'none'", "base-uri": "'self'", "form-action": "'self'",
                  "frame-ancestors": "'none'"}
    return "; ".join(f"{name} {value.strip()}" for name, value in directives.items())


DEFAULT_CSP = policy()
DASHBOARD_CSP = policy(font="data:")
INSIGHTS_CSP = policy(script=AG_STUDIO_CDN, font="data:")


def checkout_csp(sdk_url: str) -> str:
    """The checkout page's policy, for the PayPal environment that serves the SDK."""
    parts = urlsplit(sdk_url)
    paypal = f"{parts.scheme}://{parts.netloc}"
    return policy(script=paypal, connect=paypal, frame=paypal, img=paypal, font=PAYPAL_ASSETS)


def csp_for(path: str, sdk_url: str) -> str | None:
    """The policy for one path (None: FastAPI's API pages, which need their CDN)."""
    if path.startswith(API_DOCS):
        return None
    if path == "/":
        return checkout_csp(sdk_url)
    if path == "/dashboard":
        return DASHBOARD_CSP
    if path == "/insights":
        return INSIGHTS_CSP
    return DEFAULT_CSP


def forwarded_client(header: str, hops: int = TRUSTED_PROXY_HOPS) -> str | None:
    """The client's IP from X-Forwarded-For: the Nth entry from the right. Each proxy appends
    the address it saw, so entries further left are whatever the client chose to send."""
    entries = [entry.strip() for entry in header.split(",") if entry.strip()]
    return entries[-hops] if len(entries) >= hops else None


class SecurityHeaders:
    """Adds the headers to every response, and turns an unexpected error into a plain JSON 500
    (with the headers too) so a client never sees a traceback."""

    def __init__(self, app, *, production: bool, sdk_url: str):
        self.app = app
        self.production = production
        self.sdk_url = sdk_url

    def headers(self, path: str) -> dict[str, str]:
        headers = {"x-content-type-options": "nosniff",
                   "referrer-policy": "strict-origin-when-cross-origin",
                   "permissions-policy": PERMISSIONS}
        if csp := csp_for(path, self.sdk_url):
            headers["content-security-policy"] = csp
        if self.production:
            headers["strict-transport-security"] = HSTS
        return headers

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        wanted = self.headers(scope["path"])
        started = False

        async def with_headers(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                present = {name.decode("latin-1").lower() for name, _ in message["headers"]}
                message["headers"] = list(message["headers"]) + [
                    (name.encode("latin-1"), value.encode("latin-1"))
                    for name, value in wanted.items() if name not in present]  # a route's own wins
            await send(message)

        try:
            await self.app(scope, receive, with_headers)
        except Exception as err:
            log.error("Unexpected error on %s %s: %s", scope["method"], redact_path(scope["path"]), where(err))
            if started:
                raise
            body = json.dumps({"detail": "Something went wrong on our side."}).encode()
            await with_headers({"type": "http.response.start", "status": 500,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
