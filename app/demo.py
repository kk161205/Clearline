"""The demo reset: put the old test data away, then build a fresh, consistent demo story.

    python -m app.demo archive          every order and invoice archived, except KEEP; unpaid
                                        test invoices cancelled at PayPal, except KEEP
    python -m app.demo story            a new story through real Sandbox flows (see STORY)
    python -m app.demo all [--headed]   archive, then story: run it on recording day

Runs against the live site like python -m app.e2e (its settings, guards and helpers), so it needs
the same E2E_ variables and Playwright. Nothing is deleted: archived orders come back with
Include archived on /seller. Open PayPal disputes are only archived in Clearline.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import httpx

from .e2e import (OUT, PASS, Api, Config, Flow, Run, agent, assert_sandbox, report, run_flow,
                  shipped_dispute)

# Kept through every reset: the invoice that's overdue for the reminder scene.
KEEP = ["INV2-AYSR-JFEC-N8KQ-5A45"]
SETTLED = 3


def archive(api: Api, keep: list[str]) -> list[str]:
    """Cancels the unpaid test invoices (quietly), then archives everything but `keep`."""
    lines = []
    for row in api.get("/api/invoices"):
        if row["invoice_id"] in keep or row["status"] not in ("SENT", "UNPAID", "PARTIALLY_PAID", "SCHEDULED"):
            continue
        res = api.http.post(f"/api/invoices/{row['invoice_id']}/cancel")
        lines.append(f"Invoice {row['number']} ({row['invoice_id']}): cancel HTTP {res.status_code}")
    res = api.http.post("/api/archive", json={"keep": keep})
    res.raise_for_status()
    lines.append(f"Archived {res.json()['archived']} orders and invoices; kept {', '.join(keep)}.")
    return lines


def paid_waiting(run: Run, f: Flow) -> None:
    """a) Paid, waiting to ship: for shipping it live."""
    order = run.buy(f)
    f.note(f"Order …{order.id[-6:]}: paid, waiting to ship.")


def dispute(run: Run, f: Flow) -> None:
    """b) Shipped, with an item-not-received dispute and its recommendation (nothing sent)."""
    order = shipped_dispute(run, f)
    f.note(f"Order …{order.id[-6:]}: shipped, dispute open, recommendation waiting for the seller.")


def refund_to_seller(run: Run, f: Flow) -> None:
    """c) Shipped, the buyer asks for a refund as not as described: the policy leaves it to the seller."""
    order = run.buy(f)
    with f.step("mark it shipped"):
        run.ship(f, order)
    with f.step("the buyer asks for a refund: not as described"):
        run.ask_refund(f, order, reason="not_as_described")
    with f.step("it waits for the seller"):
        page = f.page("seller")
        page.wait_for_timeout(3000)
        kinds = run.api.kinds(order.id)
        assert "refund_sent" not in kinds, "It was refunded automatically"
        skipped = run.api.events(order.id, "auto_policy_skipped")
        f.note(f"Order …{order.id[-6:]}: refund request waiting for the seller"
               + (f" (policy: {skipped[-1]['payload']['reason']})." if skipped else " (policy off)."))
        run.open_order(f, order)


def settled(run: Run, f: Flow) -> None:
    """e) Paid and shipped, nothing else: what most orders look like."""
    for _ in range(SETTLED):
        order = run.buy(f)
        with f.step("mark it shipped"):
            run.ship(f, order)
        f.note(f"Order …{order.id[-6:]}: shipped, settled.")


STORY = {"paid-waiting": paid_waiting, "dispute": dispute, "refund-to-seller": refund_to_seller,
         "agent-refund": agent, "settled": settled}


def summary(api: Api) -> list[str]:
    body = api.get("/api/seller/summary")
    lines = [f"{len(body['orders'])} orders and {len(body['invoices'])} invoices on /seller:"]
    for o in body["orders"]:
        kinds = [e["kind"] for e in o["events"]]
        state = ("dispute open" if any(k.startswith("dispute") for k in kinds)
                 else "refunded" if "refund_sent" in kinds
                 else "buyer waiting" if "buyer_request" in kinds
                 else "shipped" if "tracking_added" in kinds else "paid, waiting to ship")
        lines.append(f"  …{o['order_id'][-6:]}  {state}")
    lines += [f"  Invoice {i['number']}  {i['display_status']}" for i in body["invoices"]]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.demo", description="Reset the live demo (Sandbox only).")
    parser.add_argument("action", choices=("archive", "story", "all"))
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args(argv)
    config = Config()
    assert_sandbox(httpx.get(config.base + "/api/config", timeout=120).json())
    api = Api(config)
    lines = []
    if args.action in ("archive", "all"):
        lines += archive(api, KEEP)
        print("\n".join(lines), flush=True)
    if args.action in ("story", "all"):
        from playwright.sync_api import sync_playwright
        out = Path(args.out) / ("demo-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        out.mkdir(parents=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=not args.headed)
            run = Run(config, out, browser, None, None)
            try:
                for name, fn in STORY.items():
                    run_flow(run, name, fn)
                    (out / "report.md").write_text(report(run, None), encoding="utf-8")
            finally:
                run.restore()
                browser.close()
                for state in run.states.values():
                    state.unlink(missing_ok=True)
        failed = [f.name for f in run.flows if f.status != PASS]
        lines += [f"Story: {len(run.flows) - len(failed)} of {len(run.flows)} parts done"
                  + (f"; failed: {', '.join(failed)}" if failed else "") + f". Report: {out / 'report.md'}"]
    lines += summary(api)
    print("\n".join(lines[-40:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
