// ---- Data -> view (pure; the page harness tests these) --------------------------------------

const STATE_ORDER = ["disputed", "held", "unpaid", "settled"];
const STATE_LABEL = { settled: "Settled", held: "Waiting", disputed: "Disputed", unpaid: "Unpaid" };
const PILL = { settled: "settled", held: "held", disputed: "disputed", unpaid: "" }; // unpaid = nothing yet: grey
const sumText = (m) => Object.entries(m || {}).map(([c, v]) => `<span class="num">${esc(v)} ${esc(c)}</span>`).join("") || '<span class="num">0.00</span>';
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

function kpiTiles(k) {
  const d = k.disputes, out = d.outcomes || {};
  const outcomes = Object.entries(out).map(([o, n]) => `${o} ${n}`).join(", ");
  const tiles = [
    ["settled", "Captured", sumText(k.captured), "Paid orders and invoices"],
    ["none", "Refunded", sumText(k.refunded), "Sent back to buyers"],
    ["disputed", "Open disputes", sumText(k.disputed_open), plural(k.disputes_open, "dispute open", "disputes open")],
    ["none", "Invoiced, unpaid", sumText(k.invoiced_unpaid), plural(k.invoices_unpaid, "invoice", "invoices")],
    ["held", "Overdue invoices", `<span class="num">${esc(k.invoices_overdue)}</span>`, Object.keys(k.overdue || {}).length ? `${Object.entries(k.overdue).map(([c, v]) => `${v} ${c}`).join(", ")} overdue` : "None overdue"],
    ["settled", "Auto-resolved refunds", `<span class="num">${esc(k.auto_resolved.count)}</span>`, Object.keys(k.auto_resolved.amount).length ? `${Object.entries(k.auto_resolved.amount).map(([c, v]) => `${v} ${c}`).join(", ")} by your policy` : "None yet"],
    ["settled", "Answered before a dispute", `<span class="num">${esc(k.answered_before_dispute)}</span>`, "Buyer questions that never became disputes"],
    ["disputed", "Disputes responded to", `<span class="num">${esc(d.responded)} of ${esc(d.total)}</span>`, outcomes ? `Closed: ${outcomes}` : `${d.open} still open`],
  ];
  return tiles.map(([tone, label, value, more]) => `<div class="kpi"><span class="cl-mark" data-tone="${tone}" aria-hidden="true"></span>
    <div><div class="k-label">${esc(label)}</div><div class="k-value">${value}</div><div class="k-more">${esc(more)}</div></div></div>`).join("");
}

function disputeText(d) {
  if (!d) return "";
  if (!d.open) return `${d.stage}, closed: ${d.outcome}`;
  return d.due ? `${d.stage}, respond by ${UI.date(Date.parse(d.due) / 1000)}` : `${d.stage}, open`;
}

// One grid row per money item, with sortable numbers and the last event in plain words.
function toGridRows(rows) {
  return rows.map((r) => {
    const last = r.history.length ? stop(r.history[r.history.length - 1]).title : "";
    return { ...r, amountNum: Number(r.amount), refundedNum: Number(r.refunded), dateObj: new Date(`${r.date}T00:00:00`),
             disputeText: disputeText(r.dispute), last, country: r.country || "" };
  });
}

// Next action is coloured only when it needs the seller, in the row's money-state colour.
const nextTone = (r) => (!r.needs_you ? "none" : r.state === "disputed" ? "disputed" : "held");

function drawerHtml(r) {
  const link = r.kind === "order" ? `/seller#order-${encodeURIComponent(r.ref.replace(/^…/, ""))}` : "/seller#invoices";
  return `<div class="drawer-head"><div><div class="muted small">${r.kind === "invoice" ? "Invoice" : "Order"}</div>
      <h2 id="drawer-title">${esc(r.ref)}</h2>
      <div class="num" style="font-size:var(--t-lg);font-weight:650">${esc(r.amount)} ${esc(r.currency)}</div>
      <span class="pill ${PILL[r.state]}">${esc(r.status)}</span></div>
      <button class="ghost small close" data-close aria-label="Close">Close</button></div>
    <div class="drawer-body">
      <dl class="kv">
        <dt>Next</dt><dd>${esc(r.next)}</dd>
        <dt>Date</dt><dd>${esc(UI.isoDate(r.date))}</dd>
        <dt>Item</dt><dd>${esc(r.item)}</dd>
        ${r.country ? `<dt>Country</dt><dd>${esc(r.country)}</dd>` : ""}
        ${Number(r.refunded) ? `<dt>Refunded</dt><dd class="num">${esc(r.refunded)} ${esc(r.currency)}</dd>` : ""}
        ${r.dispute ? `<dt>Dispute</dt><dd>${esc(disputeText(r.dispute))}, ${esc(r.dispute.reason.toLowerCase())}</dd>` : ""}
      </dl>
      <div><h3>History</h3>${timelineRows(r.history, `dash-${r.ref}`)}</div>
      <div class="actions"><a class="btn" href="${esc(link)}">Open on Orders</a></div>
      <p class="readonly">The dashboard is read-only. Replies, refunds and dispute answers happen on Orders, where every money action asks you first.</p>
    </div>`;
}

// ---- Grid --------------------------------------------------------------------------------------

const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
let data = null, grid = null, filterState = "all", grouped = false;
// Phones get a stacked list: every row is one full-width item (still AG Grid, so search,
// sorting, filters and grouping work the same).
let narrow = typeof window !== "undefined" && window.innerWidth < 640;
const collapsed = new Set();
const charts = {};

const columnDefs = [
  { field: "ref", headerName: "Order", minWidth: 130, filter: "agTextColumnFilter", pinned: "left",
    cellRenderer: (p) => `<span class="cl-mark" data-tone="${esc(p.data.state === "unpaid" ? "none" : p.data.state)}" style="display:inline-block;width:4px;height:18px;vertical-align:middle;margin-right:8px"></span>${esc(p.value)}` },
  { field: "dateObj", headerName: "Date", filter: "agDateColumnFilter", sort: "desc", width: 110,
    valueFormatter: (p) => (p.data ? UI.isoDate(p.data.date) : "") },
  { field: "item", headerName: "Item", flex: 1, minWidth: 180, filter: "agTextColumnFilter", tooltipField: "item" },
  { field: "amountNum", headerName: "Amount", type: "rightAligned", width: 130, filter: "agNumberColumnFilter",
    cellClass: ["num", "ag-right-aligned-cell"], valueFormatter: (p) => (p.data ? `${p.data.amount} ${p.data.currency}` : "") },
  { field: "status", headerName: "Status", width: 165, filter: "agTextColumnFilter", // fits "Paid, not shipped"
    cellRenderer: (p) => (p.data ? `<span class="pill ${PILL[p.data.state]}">${esc(p.value)}</span>` : "") },
  { field: "next", headerName: "Next action", minWidth: 210, flex: 1, filter: "agTextColumnFilter", pinned: null,
    cellRenderer: (p) => (p.data ? `<span class="next ${nextTone(p.data)}">${esc(p.value)}</span>` : "") },
  { field: "disputeText", headerName: "Dispute", minWidth: 190, flex: 1, filter: "agTextColumnFilter" },
  { field: "last", headerName: "Last event", minWidth: 190, flex: 1, filter: "agTextColumnFilter" },
  { field: "country", headerName: "Country", width: 110, filter: "agTextColumnFilter" },
  { field: "refundedNum", headerName: "Refunded", type: "rightAligned", width: 120, filter: "agNumberColumnFilter",
    cellClass: ["num", "ag-right-aligned-cell"], valueFormatter: (p) => (p.data && Number(p.data.refunded) ? `${p.data.refunded}` : "") },
];
const PHONE_HIDDEN = ["dateObj", "item", "country", "refundedNum", "status", "disputeText", "last", "next"];

function itemRenderer(p) {
  const r = p.data, el = document.createElement("div");
  el.className = "item-row";
  el.innerHTML = `<span class="cl-mark" data-tone="${esc(r.state === "unpaid" ? "none" : r.state)}" aria-hidden="true"></span>
    <span class="ref">${esc(r.ref)}</span><span class="amount num">${esc(r.amount)} ${esc(r.currency)}</span>
    <span class="next ${nextTone(r)}">${esc(r.next)}</span><span class="pill ${PILL[r.state]}">${esc(r.status)}</span>`;
  return el;
}

// Group by status (AG Grid Community has no row grouping: these are full-width header rows,
// kept at the top of their block by postSortRows; collapsing hides the block by filter).
function withGroups(rows) {
  if (!grouped) return rows;
  const out = [];
  for (const s of STATE_ORDER) {
    const mine = rows.filter((r) => r.state === s);
    if (!mine.length) continue;
    const totals = {};
    mine.forEach((r) => { totals[r.currency] = (totals[r.currency] || 0) + Number(r.amount); });
    out.push({ group: true, ref: `group-${s}`, state: s, count: mine.length,
               total: Object.entries(totals).map(([c, v]) => `${v.toFixed(2)} ${c}`).join(", ") });
    out.push(...mine);
  }
  return out;
}

function groupRenderer(p) {
  const d = p.data, open = !collapsed.has(d.state);
  const b = document.createElement("button");
  b.className = "group-row"; b.setAttribute("aria-expanded", String(open));
  b.innerHTML = `${CHEV}<span class="pill ${PILL[d.state]}">${esc(STATE_LABEL[d.state])}</span><span>${esc(plural(d.count, "item", "items"))}</span><span class="sum">${esc(d.total)}</span>`;
  b.addEventListener("click", () => { open ? collapsed.add(d.state) : collapsed.delete(d.state); grid.onFilterChanged(); grid.redrawRows(); });
  return b;
}
const CHEV = '<svg class="chev" width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';

function gridTheme() {
  return agGrid.themeQuartz.withParams({
    backgroundColor: "var(--surface)", foregroundColor: "var(--ink)", textColor: "var(--ink)", subtleTextColor: "var(--muted)",
    accentColor: "var(--settled)", borderColor: "var(--border)", chromeBackgroundColor: "var(--surface)",
    headerBackgroundColor: "var(--surface-2)", headerTextColor: "var(--muted)", headerFontWeight: 600, headerFontSize: 13,
    fontFamily: "Instrument Sans, ui-sans-serif, system-ui, sans-serif", fontSize: 14, dataFontSize: 14,
    rowHoverColor: "var(--surface-2)", selectedRowBackgroundColor: "var(--settled-tint)", oddRowBackgroundColor: "var(--surface)",
    menuBackgroundColor: "var(--surface)", wrapperBorderRadius: 14, rowHeight: 52, headerHeight: 44, spacing: 6,
    browserColorScheme: "inherit", columnBorder: false, rowBorder: { color: "var(--border)" },
    inputFocusBorder: { color: "var(--focus)", width: 2 }, focusShadow: "0 0 0 3px var(--focus)", cellHorizontalPadding: 14,
  });
}

function makeGrid() {
  grid = agGrid.createGrid(document.getElementById("grid"), {
    theme: gridTheme(), columnDefs, rowData: [], animateRows: !reduced,
    defaultColDef: { sortable: true, resizable: true, filter: true, suppressHeaderMenuButton: false },
    getRowId: (p) => p.data.ref + (p.data.group ? "" : `-${p.data.kind}`),
    isFullWidthRow: (p) => Boolean(p.rowNode.data && (p.rowNode.data.group || narrow)),
    fullWidthCellRenderer: (p) => (p.data.group ? groupRenderer(p) : itemRenderer(p)),
    getRowHeight: (p) => (p.data && p.data.group ? 44 : narrow ? 68 : 52),
    isExternalFilterPresent: () => filterState !== "all" || (grouped && collapsed.size > 0),
    doesExternalFilterPass: (node) => {
      const d = node.data;
      if (filterState !== "all" && d.state !== filterState) return false;
      return d.group || !grouped || !collapsed.has(d.state);
    },
    postSortRows: (p) => {
      if (!grouped) return;
      const rank = (n) => STATE_ORDER.indexOf(n.data.state) * 2 + (n.data.group ? 0 : 1);
      const sorted = p.nodes.map((n, i) => [n, i]).sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(([n]) => n);
      p.nodes.splice(0, p.nodes.length, ...sorted);
    },
    onRowClicked: (e) => { if (e.data && !e.data.group) openDrawer(e.data); },
    onCellKeyDown: (e) => { if (e.event && e.event.key === "Enter" && e.data && !e.data.group) openDrawer(e.data); },
    onModelUpdated: () => updateCount(),
    overlayNoRowsTemplate: '<span class="muted">No orders or invoices match.</span>',
    suppressCellFocus: false, enableCellTextSelection: true, tooltipShowDelay: 400,
  });
  fitColumns();
}

function fitColumns() {
  if (!grid) return;
  const was = narrow;
  narrow = window.innerWidth < 640;
  grid.setColumnsVisible(PHONE_HIDDEN, !narrow);
  grid.setColumnsPinned(["ref"], narrow ? null : "left");
  if (narrow) grid.sizeColumnsToFit();
  if (was !== narrow) { grid.resetRowHeights(); grid.redrawRows(); }
}

function updateCount() {
  if (!grid || !data) return;
  let n = 0;
  grid.forEachNodeAfterFilter((node) => { if (!node.data.group) n += 1; });
  document.getElementById("count").textContent = `${n} of ${data.rows.length} shown`;
}

function chipsHtml(rows) {
  const counts = { all: rows.length };
  rows.forEach((r) => { counts[r.state] = (counts[r.state] || 0) + 1; });
  return ["all", ...STATE_ORDER].map((s) => `<button class="chip" data-chip="${s}" aria-pressed="${filterState === s}">${s === "all" ? "All" : STATE_LABEL[s]}<span class="n">${counts[s] || 0}</span></button>`).join("");
}

// ---- Charts (AG Charts Community), coloured from the same tokens as everything else --------------

const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
function chartBase() {
  return {
    background: { fill: token("--surface") },
    animation: { enabled: !reduced },
    theme: { baseTheme: document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches) ? "ag-default-dark" : "ag-default",
             params: { fontFamily: "Instrument Sans, ui-sans-serif, system-ui, sans-serif", fontSize: 13,
                       foregroundColor: token("--ink"), backgroundColor: token("--surface"), accentColor: token("--settled"),
                       tooltipBackgroundColor: token("--surface"), tooltipTextColor: token("--ink") } },
    padding: { top: 8, right: 64, bottom: 8, left: 8 }, // room for the value labels
  };
}
const STATE_COLOR = () => ({ settled: token("--settled"), held: token("--held"), disputed: token("--disputed"), unpaid: token("--none") });
const CATEGORY_LABEL = { wrapping: "hyphenate", maxWidth: 96 }; // long reasons wrap, never clip
const money0 = (v) => Number(v).toLocaleString("en-GB", { minimumFractionDigits: 0, maximumFractionDigits: 0 });

function chartOptions(c) {
  const col = STATE_COLOR(), cur = c.currency;
  const label = (s) => ({ settled: "Settled", held: "Waiting", disputed: "Disputed", unpaid: "Unpaid" }[s]);
  return {
    state: { ...chartBase(), container: document.getElementById("c-state"), data: c.by_state,
      series: [{ type: "bar", direction: "horizontal", xKey: "label", yKey: "amount", yName: `Amount (${cur})`,
                 itemStyler: (p) => ({ fill: col[p.datum.state], stroke: col[p.datum.state] }), cornerRadius: 4,
                 label: { enabled: true, formatter: (p) => `${money0(p.value)} ${cur}`, color: token("--ink"), placement: "outside-end" },
                 tooltip: { renderer: (p) => ({ heading: p.datum.label, data: [{ label: "Amount", value: `${p.datum.amount.toFixed(2)} ${cur}` }] }) } }],
      axes: { y: { type: "number", position: "bottom", label: { formatter: (p) => money0(p.value) } }, x: { type: "category", position: "left", label: CATEGORY_LABEL } },
      legend: { enabled: false } },
    funnel: { ...chartBase(), container: document.getElementById("c-funnel"), data: c.funnel,
      series: [{ type: "bar", direction: "horizontal", xKey: "step", yKey: "count", yName: "Orders", cornerRadius: 4,
                 itemStyler: (p) => { const f = [token("--none"), col.settled, col.disputed][c.funnel.findIndex((x) => x.step === p.datum.step)]; return { fill: f, stroke: f }; },
                 label: { enabled: true, color: token("--ink"), placement: "outside-end" } }],
      axes: { y: { type: "number", position: "bottom" }, x: { type: "category", position: "left", label: CATEGORY_LABEL } }, legend: { enabled: false } },
    days: { ...chartBase(), container: document.getElementById("c-days"), data: c.per_day.map((d) => ({ ...d, label: UI.isoDate(d.day) })),
      series: ["settled", "held", "disputed", "unpaid"].map((s) => ({ type: "bar", xKey: "label", yKey: s, yName: label(s), stacked: true, fill: col[s], stroke: col[s] })),
      axes: { x: { type: "category", position: "bottom", label: { autoRotate: false }, interval: { step: 0 } }, y: { type: "number", position: "left", label: { formatter: (p) => money0(p.value) } } },
      legend: { position: "bottom", item: { marker: { shape: "square" } } } },
    reasons: { ...chartBase(), container: document.getElementById("c-reasons"), data: c.reasons.length ? c.reasons : [{ reason: "No disputes yet", count: 0 }],
      series: [{ type: "bar", direction: "horizontal", xKey: "reason", yKey: "count", yName: "Disputes", fill: col.disputed, stroke: col.disputed, cornerRadius: 4,
                 label: { enabled: true, color: token("--ink"), placement: "outside-end" } }],
      axes: { y: { type: "number", position: "bottom", interval: { step: 1 } }, x: { type: "category", position: "left", label: CATEGORY_LABEL } }, legend: { enabled: false } },
  };
}

// Plain-words description of each chart for screen readers (the canvas itself says nothing).
function chartSummaries(c) {
  return {
    "c-state": `Money by status in ${c.currency}: ${c.by_state.map((s) => `${s.label} ${s.amount.toFixed(2)}`).join(", ")}.`,
    "c-funnel": `Prevention funnel: ${c.funnel.map((f) => `${f.step} ${f.count}`).join(", ")}.`,
    "c-days": `Amounts per day for the last 30 days in ${c.currency}, by status.`,
    "c-reasons": c.reasons.length ? `Disputes by reason: ${c.reasons.map((r) => `${r.reason} ${r.count}`).join(", ")}.` : "No disputes yet.",
  };
}

function drawCharts() {
  if (!data || typeof agCharts === "undefined") return;
  const opts = chartOptions(data.charts);
  for (const [k, o] of Object.entries(opts)) {
    if (charts[k]) charts[k].update(o); else charts[k] = agCharts.AgCharts.create(o);
  }
  for (const [id, text] of Object.entries(chartSummaries(data.charts))) document.getElementById(id).setAttribute("aria-label", text);
  const others = data.charts.other_currencies;
  document.getElementById("chart-currency").textContent = `Amounts in ${data.charts.currency}${others.length ? `; ${others.join(", ")} items are in the table only` : ""}`;
}

// ---- Drawer ------------------------------------------------------------------------------------

let lastFocus = null;
function openDrawer(row) {
  const d = document.getElementById("drawer");
  lastFocus = document.activeElement;
  d.innerHTML = drawerHtml(row);
  d.querySelector("[data-close]").addEventListener("click", () => d.close());
  d.showModal();
}

// ---- Load and refresh ----------------------------------------------------------------------------

const rawFetch = globalThis.fetch;
globalThis.fetch = async (...args) => {
  const res = await rawFetch(...args);
  if (res.status === 401) location.href = "/login";
  return res;
};

function setRows() {
  const rows = toGridRows(data.rows);
  grid.setGridOption("rowData", withGroups(rows));
  document.getElementById("chips").innerHTML = chipsHtml(rows);
  updateCount();
}

async function load() {
  const res = await fetch("/api/dashboard");
  if (!res.ok) return;
  data = await res.json();
  document.getElementById("kpis").innerHTML = kpiTiles(data.kpis);
  document.getElementById("updated").textContent = `Updated ${UI.dateTime(data.generated_at)}`;
  if (grid) setRows();
  drawCharts();
}

function start() {
  if (typeof agGrid === "undefined" || typeof agCharts === "undefined") {
    document.getElementById("grid").innerHTML = '<div class="empty no-lib"><strong>Couldn\'t load the table and charts</strong>Reload the page to try again.</div>';
  } else {
    makeGrid();
  }
  document.getElementById("search").addEventListener("input", (e) => grid && grid.setGridOption("quickFilterText", e.target.value));
  document.getElementById("chips").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-chip]");
    if (!chip || !grid) return;
    filterState = chip.dataset.chip;
    document.querySelectorAll("[data-chip]").forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
    grid.onFilterChanged();
  });
  document.getElementById("group").addEventListener("change", (e) => { grouped = e.target.checked; collapsed.clear(); if (data && grid) setRows(); });
  document.getElementById("drawer").addEventListener("close", () => lastFocus && lastFocus.focus && lastFocus.focus());
  document.getElementById("drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") e.target.close(); }); // backdrop
  let t = 0;
  window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(fitColumns, 150); });
  // Charts draw on canvas, so they're recoloured when the theme changes.
  new MutationObserver(drawCharts).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", drawCharts);
  load().catch(() => {});
  setInterval(() => { if (!document.getElementById("drawer").open) load().catch(() => {}); }, 30000);
}

if (typeof document !== "undefined" && document.readyState !== undefined) start();
