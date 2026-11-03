"""A backup demo video: the live site walked through at a human pace, one scene at a time.

    python -m app.record [--only 1,2,5] [--out DIR]

Records each scene at 1920x1080 in the light theme with Playwright, with a visible cursor, then
burns in a caption bar and stitches everything into one MP4 with ffmpeg. Writes the per-scene
files, demo.mp4 and captions.txt (the voice-over script) to <out>/<timestamp>/.

Uses the current demo story (python -m app.demo all builds one) and changes it: scene 5 sends the
dispute evidence, scene 6 refunds, scene 8 sends a reminder. Sandbox only, with the same settings
and guards as python -m app.e2e. Passwords are only typed in an unrecorded browser; the recorded
ones reuse its sign-ins. Never part of CI. Needs Playwright and an ffmpeg with libx264 (FFMPEG,
imageio-ffmpeg, or ffmpeg on PATH).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from .e2e import SANDBOX_SITE, Api, Config, Flow, Run, assert_sandbox

OUT = r"D:\Temp\claude\clearline\recording"
SIZE = {"width": 1920, "height": 1080}
FONT = "C:/Windows/Fonts/segoeui.ttf"
PAUSE = 1000  # ms: a beat for the viewer after each action

# A cursor the video can see (headless browsers draw none); the seller's email and network address hidden.
STAGE = """
document.addEventListener('DOMContentLoaded', () => {
  const dot = document.createElement('div');
  dot.id = '__cursor';
  dot.style.cssText = 'position:fixed;z-index:2147483647;width:22px;height:22px;margin:-4px 0 0 -4px;' +
    'border-radius:50%;background:rgba(20,34,49,.35);border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4);' +
    'pointer-events:none;left:-40px;top:-40px;transition:transform .12s';
  document.body.appendChild(dot);
  addEventListener('mousemove', (e) => { dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', () => { dot.style.transform = 'scale(.7)'; }, true);
  addEventListener('mouseup', () => { dot.style.transform = ''; }, true);
  const style = document.createElement('style');
  style.textContent = '#account-email, #account-email-full { filter: blur(7px); }' +
    '#system-checks dt:last-of-type, #system-checks dd:last-of-type { display: none; }'; // the viewer's own network address
  document.head.appendChild(style);
});
"""


@dataclass
class Scene:
    number: int
    title: str
    caption: str
    run: object
    videos: list = field(default_factory=list)  # (path, start, end) in seconds of that file
    status: str = "PASS"
    detail: str = ""


class Stage:
    """The recording session: the guards and helpers of app.e2e, recorded contexts at 1920x1080."""

    def __init__(self, run: Run, out: Path):
        self.run, self.out, self.config, self.api = run, out, run.config, run.api
        self.flow = Flow("record", run, out / "shots")
        self.flow.dir.mkdir(parents=True, exist_ok=True)
        self.order_id = None  # the order bought in scene 2, shipped in scene 4
        self.begin = None

    def context(self, who: str, scene_dir: Path):
        state = self.run.states.get(who)
        context = self.run.browser.new_context(
            viewport=SIZE, record_video_dir=str(scene_dir), record_video_size=SIZE, color_scheme="light",
            storage_state=str(state) if state and state.exists() else None)
        context.add_init_script(STAGE)
        self.run.watch(context, self.flow)
        return context

    # ---- Human-paced actions -------------------------------------------------------------------

    def ready(self):
        """The scene's first page has loaded: the video starts here, not on the blank new tab."""
        if self.begin is None:
            self.begin = time.monotonic()

    def point(self, page, locator, *, click=True):
        self.ready()
        locator.first.evaluate("e => e.scrollIntoView({block: 'center', behavior: 'instant'})")  # clear of the caption bar
        page.wait_for_timeout(300)
        box = locator.first.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=30)
        page.wait_for_timeout(350)
        if click:
            page.mouse.down(); page.mouse.up()
        page.wait_for_timeout(PAUSE)

    def type(self, page, locator, text: str):
        self.point(page, locator)
        locator.first.press_sequentially(text, delay=90)
        page.wait_for_timeout(PAUSE // 2)

    def glide(self, page, pixels: int, steps: int = 12):
        for _ in range(steps):
            page.mouse.wheel(0, pixels / steps)
            page.wait_for_timeout(60)
        page.wait_for_timeout(PAUSE)

    # ---- Sign-ins, off camera -------------------------------------------------------------------

    def sign_in(self):
        context = self.run.browser.new_context()
        page = context.new_page()
        page.goto(self.config.base + "/login")
        page.fill("input[type=email], input[name=email]", self.config.seller_email)
        page.fill("input[type=password]", self.config.seller_password)
        page.click("button[type=submit]")
        page.wait_for_url("**/seller")
        context.storage_state(path=str(self.run.states["seller"]))
        page.goto(SANDBOX_SITE + "/signin", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        for _ in range(3):
            self.run.challenge_check(page, self.flow)
            if not self.run.paypal_login(page, self.flow):
                break
            page.wait_for_timeout(4000)
        context.storage_state(path=str(self.run.states["buyer"]))
        context.close()

    # ---- Finding the story's orders -------------------------------------------------------------

    def orders(self) -> list[dict]:
        return self.api.get("/api/seller/summary")["orders"]

    def find(self, test, what: str) -> str:
        for o in self.orders():
            if test(o, [e["kind"] for e in o["events"]], self.api.kinds(o["order_id"])):
                return o["order_id"]
        raise LookupError(f"No {what} in the current story: run python -m app.demo all")


def card(page, order_id: str):
    return page.locator(f'article.order[data-order-id="{order_id}"]')


def open_card(stage: Stage, page, order_id: str):
    row = card(page, order_id)
    row.wait_for(state="visible", timeout=60000)
    head = row.locator(".order-head")
    if head.get_attribute("aria-expanded") != "true":
        stage.point(page, head)
    row.locator(".history").wait_for(state="visible", timeout=30000)
    return row


# ---- The scenes ------------------------------------------------------------------------------------

def landing(stage: Stage, page):
    page.goto(stage.config.base + "/", wait_until="load")
    stage.ready()
    page.wait_for_timeout(1500)
    stage.point(page, page.get_by_role("link", name="Try the shop"), click=False)
    stage.point(page, page.locator(".route").nth(2), click=False)


def buy(stage: Stage, page):
    page.goto(stage.config.base + "/shop", wait_until="load")
    button = page.locator("paypal-button")
    button.wait_for(state="visible", timeout=60000)
    stage.ready()
    page.wait_for_timeout(PAUSE)
    created = {}
    page.on("response", lambda r: created.update(id=r.json().get("orderId"))
            if r.request.method == "POST" and r.url.endswith("/api/orders") and r.status == 200 else None)
    with page.expect_popup(timeout=30000) as opening:
        stage.point(page, button)
    popup = opening.value
    stage.popup = (popup, time.monotonic())
    popup.wait_for_timeout(2500)
    stage.run.approve_popup(popup, stage.flow)
    stage.popup_closed = time.monotonic()
    page.locator("#paid").wait_for(state="visible", timeout=60000)
    page.wait_for_timeout(PAUSE * 2)
    stage.order_id = created.get("id")
    stage.point(page, page.locator("#paid-help"))
    page.wait_for_load_state("load")
    page.wait_for_timeout(PAUSE * 3)


def today(stage: Stage, page):
    page.goto(stage.config.base + "/seller", wait_until="load")
    page.locator("ul.attention").wait_for(timeout=60000)
    stage.ready()
    page.wait_for_timeout(PAUSE)
    for item in page.locator("ul.attention li").all()[:4]:
        stage.point(page, item, click=False)


def ship(stage: Stage, page):
    order_id = stage.order_id or stage.find(lambda o, k, full: "payment_captured" in k and "tracking_added" not in k
                                            and not any(x.startswith("dispute") for x in k) and "refund_sent" not in k,
                                            "order waiting to ship")
    page.goto(stage.config.base + "/seller", wait_until="load")
    page.locator("#order-list article").first.wait_for(timeout=60000)
    stage.ready()
    row = open_card(stage, page, order_id)
    form = row.locator("form.ship")
    stage.type(page, form.locator("[name=tracking_number]"), "1234567890")
    form.locator("[name=carrier]").select_option("DHL")
    stage.point(page, form.locator("button"))
    page.get_by_text("Marked as shipped").first.wait_for(timeout=30000)
    page.wait_for_timeout(PAUSE * 2)


def dispute(stage: Stage, page):
    order_id = stage.find(lambda o, k, full: "ai_recommendation" in full and "dispute_action_sent" not in full
                          and not any(x == "dispute_resolved" for x in k), "dispute with a recommendation to approve")
    page.goto(stage.config.base + "/seller", wait_until="load")
    page.locator("#order-list article").first.wait_for(timeout=60000)
    stage.ready()
    row = open_card(stage, page, order_id)
    ai = row.locator(".ai")
    ai.scroll_into_view_if_needed()
    page.wait_for_timeout(PAUSE)
    for part in (ai.locator(".ai-head"), ai.locator("ul, ol").first, ai.locator('[data-field="message_to_buyer"]')):
        if part.count():
            stage.point(page, part, click=False)
    stage.point(page, row.locator("[data-approve]"))
    stage.point(page, page.locator("[data-ok]"))
    row.get_by_text("Evidence sent").first.wait_for(timeout=60000)
    stage.point(page, row.get_by_text("Evidence sent").first, click=False)
    page.wait_for_timeout(PAUSE * 2)


def refund(stage: Stage, page):
    order_id = stage.find(lambda o, k, full: "tracking_added" in k and "buyer_request" in k and "refund_sent" not in k,
                          "shipped order with a refund request")
    link = stage.api.get(f"/api/orders/{order_id}/buyer")["link"]
    page.goto(link if link.startswith("http") else stage.config.base + link, wait_until="load")
    stage.ready()
    page.wait_for_timeout(PAUSE)
    stage.glide(page, 500)
    page.goto(stage.config.base + "/seller", wait_until="load")
    page.locator("#order-list article").first.wait_for(timeout=60000)
    stage.ready()
    row = open_card(stage, page, order_id)
    buyer = row.locator(".buyer")
    buyer.scroll_into_view_if_needed()
    stage.point(page, buyer.locator(".req").first, click=False)
    stage.point(page, buyer.locator('[data-refund="partial"]').first)
    stage.type(page, page.locator("[data-input]"), "40.00")
    stage.point(page, page.locator("[data-ok]"))
    page.get_by_text("Refund of 40.00 USD sent").first.wait_for(timeout=60000)
    page.wait_for_timeout(PAUSE * 2)


def agent(stage: Stage, page):
    order_id = stage.find(lambda o, k, full: "refund_sent" in k and any(
        e["kind"] == "buyer_request" and e["payload"].get("via") == "agent" for e in o["events"]), "agent refund")
    page.goto(stage.config.base + "/seller", wait_until="load")
    page.locator("#order-list article").first.wait_for(timeout=60000)
    stage.ready()
    row = open_card(stage, page, order_id)
    for stop in row.locator(".history li").all()[-4:]:
        stage.point(page, stop, click=False)
    stage.point(page, row.locator(".buyer").get_by_text("via AI agent").first, click=False)


def invoice(stage: Stage, page):
    page.goto(stage.config.base + "/seller", wait_until="load")
    item = page.locator("ul.attention li").filter(has_text="overdue").first
    item.wait_for(timeout=60000)
    stage.point(page, item.locator("button"))
    cell = page.locator("[data-reminder]").filter(has=page.locator("[data-rem-send]")).first
    cell.locator("[data-rem-send]").wait_for(state="visible", timeout=90000)
    stage.point(page, cell.locator("textarea, input").first, click=False)
    stage.point(page, cell.locator("[data-rem-send]"))
    stage.point(page, page.locator("[data-ok]"))
    page.get_by_text("Reminder sent").first.wait_for(timeout=60000)
    page.wait_for_timeout(PAUSE * 2)


def dashboard(stage: Stage, page):
    page.goto(stage.config.base + "/dashboard", wait_until="load")
    page.locator(".kpi").first.wait_for(timeout=60000)
    stage.ready()
    page.wait_for_timeout(PAUSE * 2)
    for kpi in page.locator(".kpi").all()[:3]:
        stage.point(page, kpi, click=False)
    stage.glide(page, 900, steps=18)


def insights(stage: Stage, page):
    saved = stage.api.layout()
    try:
        page.goto(stage.config.base + "/insights", wait_until="load")
        page.locator("canvas").first.wait_for(state="visible", timeout=60000)
        page.wait_for_timeout(PAUSE)
        stage.point(page, page.get_by_role("button", name="Edit layout"))
        box = page.locator("textarea.ag-studio-ai-assistant-textarea")
        if not box.is_visible():
            stage.point(page, page.locator(".ag-studio-panel-header", has_text="AI Assistant"))
        for question in ("Which orders need me this week?", "Add a bar chart of refunded amount by buyer country"):
            stage.point(page, page.locator(".ag-studio-ai-thread-new-button"))
            stage.type(page, box, question)
            stage.point(page, page.locator(".ag-studio-ai-assistant-send-button"))
            stop = page.get_by_role("button", name="Stop")
            stop.first.wait_for(state="visible", timeout=30000)
            deadline = time.monotonic() + 180
            while stop.first.is_visible() and time.monotonic() < deadline:
                page.wait_for_timeout(500)
            page.wait_for_timeout(PAUSE * 3)
    finally:
        stage.api.restore_layout(saved)


def system(stage: Stage, page):
    page.goto(stage.config.base + "/seller#settings", wait_until="load")
    page.locator("#order-list article").first.wait_for(timeout=60000)
    stage.point(page, page.locator("#system summary"))
    page.locator("#system-checks dt").first.wait_for(timeout=30000)
    page.locator("#system-checks").evaluate("e => e.scrollIntoView({block: 'center', behavior: 'smooth'})")
    page.wait_for_timeout(PAUSE * 3)


SCENES = [
    (1, "Landing page", "seller", "Clearline: see where every PayPal payment stands.", landing),
    (2, "A buyer pays", "buyer", "A buyer pays with PayPal and gets a private page for their order.", buy),
    (3, "Today", "seller", "Sign in, and Today shows only what needs you.", today),
    (4, "Ship", "seller", "Add tracking in one step. The buyer's page follows.", ship),
    (5, "Dispute", "seller", "AI reads the dispute and drafts the answer. You approve it.", dispute),
    (6, "Refund request", "seller", "Buyers ask before they dispute. You decide, even a partial refund.", refund),
    (7, "Agent refund", "seller", "A buyer's AI agent asked for a refund. Your rules refunded it at once.", agent),
    (8, "Overdue invoice", "seller", "An invoice is overdue. AI drafts a polite reminder. You send it.", invoice),
    (9, "Money in flight", "seller", "All the money in flight, in one view.", dashboard),
    (10, "Insights", "seller", "Ask about your shop in plain words. Get answers and charts.", insights),
    (11, "System status", "seller", "Checked and green before you go live.", system),
]


def record(stage: Stage, number: int, title: str, who: str, caption: str, fn) -> Scene:
    scene = Scene(number, title, caption, fn)
    folder = stage.out / f"{number:02d}-raw"
    context = stage.context(who, folder)
    page = context.new_page()
    stage.popup, stage.begin = None, None
    started = time.monotonic()
    print(f"== scene {number}: {title}", flush=True)
    try:
        fn(stage, page)
    except Exception as err:
        scene.status, scene.detail = "FAIL", f"{type(err).__name__}: {str(err)[:240]}"
        stage.flow.shot(f"scene {number} failed", page)
    finally:
        ended = time.monotonic()
        popup = stage.popup
        context.close()
    main = Path(page.video.path())
    start = (stage.begin - started - 0.3) if stage.begin else 0  # from the first loaded page
    if popup:  # the PayPal popup is its own video: the buyer's page until the click, the popup, then back
        shown, opened = popup[0], popup[1] - started
        closed = getattr(stage, "popup_closed", ended) - started
        scene.videos = [(main, start, opened), (Path(shown.video.path()), 0, closed - opened), (main, closed, ended - started)]
    else:
        scene.videos = [(main, start, ended - started)]
    print(f"   {scene.status} {scene.detail}", flush=True)
    return scene


# ---- Video ------------------------------------------------------------------------------------------

def ffmpeg_exe() -> str:
    if os.environ.get("FFMPEG"):
        return os.environ["FFMPEG"]
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        found = shutil.which("ffmpeg")
        if not found:
            raise SystemExit("No ffmpeg: set FFMPEG, or install imageio-ffmpeg on PYTHONPATH.")
        return found


def wrap(text: str, width: int = 70) -> str:
    """At most two lines."""
    words, lines = text.split(), [""]
    for word in words:
        if len(lines[-1]) + len(word) + 1 > width and len(lines) < 2:
            lines.append("")
        lines[-1] = f"{lines[-1]} {word}".strip()
    return "\n".join(lines)


def escaped(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:")


def burn(ffmpeg: str, scene: Scene, out: Path) -> Path:
    """The scene's clips joined, scaled to 1920x1080, with the caption bar at the bottom."""
    caption = out / f"{scene.number:02d}-caption.txt"
    caption.write_text(wrap(scene.caption), encoding="utf-8")
    inputs, parts = [], []
    for i, (path, start, end) in enumerate(scene.videos):
        inputs += ["-ss", f"{max(start, 0):.2f}", "-t", f"{max(end - start, 0.5):.2f}", "-i", str(path)]
        parts.append(f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=white,"
                     f"fps=30,setsar=1[v{i}]")
    joined = "".join(f"[v{i}]" for i in range(len(parts)))
    text = (f"drawbox=x=0:y=ih-150:w=iw:h=150:color=0x142231@0.88:t=fill,"
            f"drawtext=fontfile='{escaped(Path(FONT))}':textfile='{escaped(caption)}':fontcolor=white:fontsize=40:"
            f"line_spacing=10:x=(w-text_w)/2:y=h-75-text_h/2")
    graph = ";".join(parts) + f";{joined}concat=n={len(parts)}:v=1:a=0[c];[c]{text}[out]"
    target = out / f"{scene.number:02d}-{scene.title.lower().replace(' ', '-')}.mp4"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", *inputs, "-filter_complex", graph, "-map", "[out]",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", str(target)], check=True)
    caption.unlink()
    return target


def stitch(ffmpeg: str, clips: list[Path], out: Path) -> Path:
    listing = out / "clips.txt"
    listing.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    target = out / "demo.mp4"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", str(target)], check=True, cwd=out)
    listing.unlink()
    return target


def length(ffmpeg: str, path: Path) -> float:
    probe = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True)
    stamp = probe.stderr.split("Duration: ")[1].split(",")[0]
    h, m, s = stamp.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.record", description="Record the backup demo (Sandbox only).")
    parser.add_argument("--only", help="scene numbers, e.g. 1,3,9")
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args(argv)
    wanted = {int(n) for n in args.only.split(",")} if args.only else {s[0] for s in SCENES}
    config = Config()
    for attempt in range(4):  # the first connection sometimes gets reset; the next one works
        try:
            assert_sandbox(httpx.get(config.base + "/api/config", timeout=120).json())
            break
        except httpx.TransportError:
            if attempt == 3:
                raise
            time.sleep(5)
    ffmpeg = ffmpeg_exe()
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    from playwright.sync_api import sync_playwright
    scenes = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True, slow_mo=60)
        run = Run(config, out, browser, None, None)
        stage = Stage(run, out)
        try:
            stage.sign_in()
            for number, title, who, caption, fn in SCENES:
                if number in wanted:
                    scenes.append(record(stage, number, title, who, caption, fn))
        finally:
            run.restore()
            browser.close()
            for state in run.states.values():  # login cookies
                state.unlink(missing_ok=True)
    clips = [burn(ffmpeg, s, out) for s in scenes]
    film = stitch(ffmpeg, clips, out) if clips else None
    (out / "captions.txt").write_text("".join(
        f"Scene {s.number}, {s.title} ({length(ffmpeg, c):.0f} s)\n{s.caption}\n\n" for s, c in zip(scenes, clips)), encoding="utf-8")
    for raw in out.glob("*-raw"):
        shutil.rmtree(raw)
    report = [f"{s.number:>2} {s.title:<16} {s.status} {s.detail}" for s in scenes]
    print("\n".join(report))
    if film:
        print(f"\n{film}  ({length(ffmpeg, film):.0f} s)")
    (out / "scenes.json").write_text(json.dumps([{"scene": s.number, "title": s.title, "status": s.status,
                                                  "detail": s.detail} for s in scenes], indent=1), encoding="utf-8")
    return 0 if all(s.status == "PASS" for s in scenes) else 1


if __name__ == "__main__":
    sys.exit(main())
