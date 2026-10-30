"""Day 8: Insights (AG Studio). The page, its saved layouts, the licence key endpoint, and the
report it builds from the dashboard's PII-free data."""

import json
import logging
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tests.support import make_client
from tests.test_dashboard import CLIENT, EMAIL, NOTE, mixed  # noqa: F401  (fixture)

ROOT = Path(__file__).parent.parent
NODE = shutil.which("node")
FAKE_KEY = "fake-ag-studio-key-for-tests-only"


def test_insights_needs_the_seller_login(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    assert client.get("/insights", follow_redirects=False).status_code == 303
    for method, path in [("GET", "/api/insights/layout"), ("PUT", "/api/insights/layout"),
                         ("DELETE", "/api/insights/layout"), ("GET", "/api/insights/license")]:
        assert client.request(method, path, json={}).status_code == 401, path


def test_layout_is_saved_per_seller_in_its_own_table(tmp_path):
    client, settings = make_client(tmp_path)
    assert client.get("/api/insights/layout").json() == {"state": None, "updated_at": None}
    state = {"pages": [{"id": "money", "widgets": {}, "widgetLayout": {}}], "selectedPageId": "money"}
    saved = client.put("/api/insights/layout", json=state)
    assert saved.status_code == 200
    got = client.get("/api/insights/layout").json()
    assert got["state"] == state and got["updated_at"] == saved.json()["updated_at"]
    with sqlite3.connect(settings.database_path) as db:  # a proper table, not app_settings
        assert db.execute("SELECT user_key FROM insights_layouts").fetchall() == [("seller",)]
        assert db.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0] == 0
    client.put("/api/insights/layout", json={**state, "selectedPageId": "other"})  # overwrites
    assert client.get("/api/insights/layout").json()["state"]["selectedPageId"] == "other"
    assert client.delete("/api/insights/layout").json() == {"ok": True}  # Reset to default
    assert client.get("/api/insights/layout").json()["state"] is None


@pytest.mark.parametrize("body", [
    b"[1, 2, 3]", b'"a string"', b"42", b"null", b"not json {",
    json.dumps({"pages": ["x" * (256 * 1024)]}).encode(),  # over 256 KB
], ids=["array", "string", "number", "null", "not-json", "too-large"])
def test_layout_must_be_a_json_object_of_at_most_256_kb(tmp_path, body):
    client, _ = make_client(tmp_path)
    res = client.put("/api/insights/layout", content=body, headers={"Content-Type": "application/json"})
    assert res.status_code == 422
    assert client.get("/api/insights/layout").json()["state"] is None


def test_licence_key_endpoint(tmp_path, caplog):
    client, _ = make_client(tmp_path)
    res = client.get("/api/insights/license")
    assert res.json() == {"key": None} and res.headers["cache-control"] == "no-store"

    caplog.set_level(logging.DEBUG)
    (tmp_path / "k").mkdir()
    keyed, _ = make_client(tmp_path / "k", ag_studio_license_key=FAKE_KEY)
    res = keyed.get("/api/insights/license")
    assert res.json() == {"key": FAKE_KEY} and res.headers["cache-control"] == "no-store"
    page = keyed.get("/insights")
    assert page.status_code == 200 and FAKE_KEY not in page.text  # never in the HTML
    assert FAKE_KEY not in caplog.text  # never logged


def test_insights_page_loads_ag_studio_pinned_with_sri_and_a_failure_state(tmp_path):
    client, _ = make_client(tmp_path)
    html = client.get("/insights").text
    tag = re.search(r'<script src="https://cdn\.jsdelivr\.net/[^>]+>', html).group(0)
    assert "ag-studio@3.0.0/dist/umd/ag-studio.min.js" in tag
    assert 'integrity="sha384-' in tag and 'crossorigin="anonymous"' in tag and "onerror=" in tag
    assert len(re.findall(r'<script src="https://', html)) == 1  # the one third-party script
    assert "Insights couldn't load" in html and 'href="/dashboard"' in html
    for page in ("seller.html", "dashboard.html", "agents.html"):  # in every seller nav
        assert 'href="/insights"' in (ROOT / "app/static" / page).read_text(encoding="utf-8")


def insights_view(data):
    out = subprocess.run([NODE, str(ROOT / "tests/js/insights_harness.js"), str(ROOT / "app/static/insights.html")],
                         input=json.dumps(data), capture_output=True, text=True, encoding="utf-8",
                         timeout=30, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_insights_report_is_built_from_the_dashboard_data(mixed):  # noqa: F811
    client, _ = mixed
    data = client.get("/api/dashboard").json()
    view = insights_view(data)
    sources = {s["id"]: s for s in view["sources"]}
    assert set(sources) == {"money", "daily", "disputes"}

    # Same rows and amounts as the dashboard (main currency), and no PII anywhere.
    assert len(sources["money"]["data"]) == len(data["rows"])
    assert sum(r["amount"] for r in sources["money"]["data"]) == sum(float(r["amount"]) for r in data["rows"])
    assert {r["state"] for r in sources["money"]["data"]} <= {"Settled", "Waiting", "Disputed", "Unpaid"}
    assert len(sources["disputes"]["data"]) == data["kpis"]["disputes"]["total"]
    dump = json.dumps(view)
    for secret in (EMAIL, CLIENT, NOTE, "ORDER0000000PLAIN"):
        assert secret not in dump

    # Every field the default report uses exists in its source; every row field is declared.
    fields = {f"{s['id']}.{f['id']}" for s in view["sources"] for f in s["fields"]}
    used = set(re.findall(r'"id": "((?:money|daily|disputes)\.[a-z_]+)"', json.dumps(view["defaultState"], indent=0)))
    assert used and used <= fields, used - fields
    for s in view["sources"]:
        declared = {f["id"] for f in s["fields"]}
        assert all(set(row) <= declared for row in s["data"]), s["id"]

    # Our custom widget is in the default report; phones get it stacked full width, no overlaps.
    page = view["defaultState"]["pages"][0]
    assert page["widgets"]["money-state"]["type"] == "money-state"
    stacked = view["phoneState"]["pages"][0]["widgetLayout"]
    assert set(stacked) == set(page["widgetLayout"])
    spans = sorted((w["yTrack"], w["yTrack"] + w["ySpan"]) for w in stacked.values())
    assert all(w["xTrack"] == 0 and w["xSpan"] == 24 for w in stacked.values())
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))

    # Money state: status colours and shares, zero amounts left out.
    ms = view["moneyState"]
    assert 'data-tone="settled"' in ms and 'data-tone="disputed"' in ms and "Waiting" not in ms
    assert "75%" in ms and "25%" in ms and "400.00" in ms


def test_insights_money_uses_the_apps_two_decimal_format():
    html = (ROOT / "app/static/insights.html").read_text(encoding="utf-8")
    assert 'const MONEY = { format: "0.00" };' in html
    assert "createFormats({ overrides: { currencyFormat: { formatOptions: MONEY }, decimalFormat: { formatOptions: MONEY } } })" in html
    fields = re.findall(r'format: "currencyFormat"[^}]*}', html)
    assert fields and all("formatOptions: MONEY" in f for f in fields)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_grid_amount_stays_numeric_and_displays_like_the_app(mixed):  # noqa: F811
    client, _ = mixed
    view = insights_view(client.get("/api/dashboard").json())
    money = next(s for s in view["sources"] if s["id"] == "money")
    assert all(isinstance(r["amount"], (int, float)) for r in money["data"])  # numbers: they sort and sum
    assert "amount_label" not in money["data"][0]
    # Every money field shows as the app does, in cells and totals alike.
    assert view["formatted"]["money.amount"] == ["1140.00 USD", "900.00 USD", "0.50 USD", ""]
    assert set(view["formatted"]) >= {"money.amount", "money.refunded", "money.net", "daily.settled", "disputes.amount"}
    grid = view["defaultState"]["pages"][0]["widgets"]["items"]
    assert grid["dataMapping"]["cols"][1] == {"id": "money.amount", "aggregation": "sum"}
    assert grid["sort"] == [{"field": {"id": "money.amount", "aggregation": "sum"}, "direction": "desc"}]
    amounts = sorted([900, 1140, 85.5], reverse=True)
    assert amounts[0] == 1140  # numeric order: 1140 above 900 (as text, "900.00" would sort first)


def test_studios_own_widgets_stay_available_beside_ours():
    html = (ROOT / "app/static/insights.html").read_text(encoding="utf-8")
    assert "const widgets = (config) => agStudio.createWidgets({" in html
    assert 'menu: [...config.menu, { label: "Clearline", widgetIds: ["money-state"] }],' in html
    assert "maxTurns: 8," in html  # Studio's own cap; the server keeps plain questions to 6
