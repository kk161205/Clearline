const esc = UI.esc;
const STATE_LABEL = { settled: "Settled", held: "Waiting", disputed: "Disputed", unpaid: "Unpaid" };
const STATE_TONE = { Settled: "settled", Waiting: "held", Disputed: "disputed", Unpaid: "none" };
const FONT = "Instrument Sans, ui-sans-serif, system-ui, sans-serif";
// Money as everywhere else in Clearline: two decimals, no thousands separator ("1200.00").
const MONEY = { format: "0.00" };

// ---- Data: the same PII-free rows as /api/dashboard, in the main currency -----------------

const MONEY_FIELDS = [
  { id: "ref", name: "Order", format: "textFormat", cardinality: "high" },
  { id: "kind", name: "Kind", format: "textFormat" },
  { id: "date", name: "Date", format: "dateFormat", formatOptions: { format: "d mmm" } },
  { id: "item", name: "Item", format: "textFormat" },
  { id: "country", name: "Buyer country", format: "textFormat" },
  { id: "state", name: "Money state", format: "textFormat" },
  { id: "status", name: "Status", format: "textFormat" },
  { id: "next", name: "Next action", format: "textFormat" },
  { id: "needs_you", name: "Needs you", format: "booleanFormat" },
  { id: "deadline", name: "Deadline", format: "dateFormat", formatOptions: { format: "d mmm" } },
  // The date to filter on for "needs me / this week / today": the deadline, or today when
  // the item waits on the seller with no deadline; empty when it isn't the seller's turn.
  { id: "act_by", name: "Act by", format: "dateFormat", formatOptions: { format: "d mmm" } },
  { id: "amount", name: "Amount", format: "currencyFormat", formatOptions: MONEY },
  { id: "refunded", name: "Refunded", format: "currencyFormat", formatOptions: MONEY },
  { id: "net", name: "Net of refunds", format: "currencyFormat", formatOptions: MONEY },
];
// Disputes only, so dispute charts aren't swamped by orders that never had one.
const DISPUTE_FIELDS = [
  { id: "ref", name: "Order", format: "textFormat", cardinality: "high" },
  { id: "stage", name: "Stage", format: "textFormat" },
  { id: "reason", name: "Reason", format: "textFormat" },
  { id: "outcome", name: "Outcome", format: "textFormat" },
  { id: "amount", name: "Disputed amount", format: "currencyFormat", formatOptions: MONEY },
];
const DAILY_FIELDS = [
  { id: "day", name: "Day", format: "dateFormat", formatOptions: { format: "d mmm" } },
  ...["settled", "waiting", "disputed", "unpaid"].map((s) => ({ id: s, name: s[0].toUpperCase() + s.slice(1), format: "currencyFormat", formatOptions: MONEY })),
];

// Pure: "900.00 USD" for a number (blank stays blank).
const moneyText = (value, cur) => (value == null || value === "" || isNaN(Number(value)) ? "" : `${Number(value).toFixed(2)} ${cur}`);
const moneyFormatter = (cur) => () => (value) => moneyText(value, cur);

// Pure: YYYY-MM-DD in the browser's own calendar, the one Studio's relative date filters use.
const localDay = (at = new Date()) => `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, "0")}-${String(at.getDate()).padStart(2, "0")}`;

// Pure: Studio sources from an /api/dashboard payload (the page harness tests this). Act by is
// worked out again here in the browser's calendar: the server's "today" is UTC, so for a seller in
// India "this week" on a Monday morning would otherwise miss everything due "today".
function toSources(d, today = localDay()) {
  const actBy = (r) => (!r.needs_you ? null : r.deadline && r.deadline.slice(0, 10) > today ? r.deadline.slice(0, 10) : today);
  const cur = d.charts.currency;
  const money = d.rows.filter((r) => r.currency === cur).map((r) => ({
    ref: r.ref, kind: r.kind === "invoice" ? "Invoice" : "Order", date: r.at ? localDay(new Date(r.at * 1000)) : r.date, item: r.item,
    country: r.country || "Unknown", state: STATE_LABEL[r.state], status: r.status, next: r.next,
    needs_you: Boolean(r.needs_you), deadline: r.deadline || null, act_by: actBy(r), amount: Number(r.amount), refunded: Number(r.refunded),
    net: Number(r.amount) - Number(r.refunded),
  }));
  const disputes = d.rows.filter((r) => r.dispute && r.currency === cur).map((r) => ({
    ref: r.ref, stage: r.dispute.stage, reason: r.dispute.reason,
    outcome: r.dispute.open ? "Open" : r.dispute.outcome, amount: Number(r.dispute.amount),
  }));
  const daily = d.charts.per_day.map((p) => ({ day: p.day, settled: p.settled, waiting: p.held, disputed: p.disputed, unpaid: p.unpaid }));
  // Money fields stay numbers (so they sort and sum) and display as "900.00 USD", the app's
  // format, wherever Studio shows them: cells, totals, values, tooltips.
  const shown = (fields) => fields.map((fd) => (fd.format === "currencyFormat" ? { ...fd, createValueFormatter: moneyFormatter(cur) } : fd));
  return [{ id: "money", data: money, fields: shown(MONEY_FIELDS) }, { id: "daily", data: daily, fields: shown(DAILY_FIELDS) },
          { id: "disputes", data: disputes, fields: shown(DISPUTE_FIELDS) }];
}

// ---- The default report: what a seller sees before arranging their own ----------------------

const f = (id, aggregation) => (aggregation ? { id, aggregation } : { id });
const kpi = (id, aggregation, text) => ({ type: "value", dataMapping: { value: [f(id, aggregation)] }, format: { caption: { enabled: true, text } } });
// Fits a report canvas from about 460px (1280px screen, assistant open) to 1550px wide:
// titles at 18px, three KPIs, and a full-width grid with three columns.
const title = (text) => ({ title: { enabled: true, text, typography: { fontSize: 18 } } });
const defaultState = (cur) => ({
  pages: [{
    id: "money",
    widgets: {
      "kpi-amount": kpi("money.amount", "sum", "Amount"),
      "kpi-refunded": kpi("money.refunded", "sum", "Refunded"),
      "kpi-net": kpi("money.net", "sum", "Net of refunds"),
      "money-state": { type: "money-state", dataMapping: { state: [f("money.state")], value: [f("money.net", "sum")] },
                       format: title("Money state") },
      "per-day": { type: "column-chart-stacked",
                   dataMapping: { categoryKey: [f("daily.day")], valueKey: ["settled", "waiting", "disputed", "unpaid"].map((s) => f(`daily.${s}`, "sum")) },
                   format: title(`Per day, ${cur}`) },
      "items": { type: "grid", dataMapping: { cols: [f("money.ref"), f("money.amount", "sum"), f("money.next")] },
                 sort: [{ field: f("money.amount", "sum"), direction: "desc" }],
                 format: title("Orders and invoices") },
      "state-filter": { type: "list-filter", dataMapping: { value: [f("money.state")] }, format: title("Show") },
      "reasons": { type: "bar-chart-grouped", dataMapping: { categoryKey: [f("disputes.reason")], valueKey: [f("disputes.ref", "countd")] },
                   format: title("Disputes by reason") },
    },
    widgetLayout: {
      "kpi-amount": { xTrack: 0, yTrack: 0, xSpan: 8, ySpan: 5 },
      "kpi-refunded": { xTrack: 8, yTrack: 0, xSpan: 8, ySpan: 5 },
      "kpi-net": { xTrack: 16, yTrack: 0, xSpan: 8, ySpan: 5 },
      "money-state": { xTrack: 0, yTrack: 5, xSpan: 12, ySpan: 14 },
      "per-day": { xTrack: 12, yTrack: 5, xSpan: 12, ySpan: 14 },
      "items": { xTrack: 0, yTrack: 19, xSpan: 24, ySpan: 15 },
      "reasons": { xTrack: 0, yTrack: 34, xSpan: 16, ySpan: 13 },
      "state-filter": { xTrack: 16, yTrack: 34, xSpan: 8, ySpan: 13 },
    },
  }],
  selectedPageId: "money",
  panels: { filters: { collapsed: true }, data: { collapsed: true } },
});

// ---- "Money state": our custom widget -------------------------------------------------------

class MoneyStateWidget {
  init(p) { this.el = document.createElement("div"); this.el.className = "ms"; this.refresh(p); }
  getGui() { return this.el; }
  destroy() {}
  async refresh(p) {
    const cat = (p.dataMapping.state || [])[0], val = (p.dataMapping.value || [])[0];
    if (!cat || !val) { p.widgetApi.setDisplayState("incompleteDataMapping"); return; }
    p.widgetApi.setDisplayState("loading", { prominent: false });
    try {
      const res = await p.widgetApi.getData({ kind: "flat", fields: [cat, val] });
      const rows = (res.results && res.results.rows) || [];
      if (!rows.length) { p.widgetApi.setDisplayState("noData"); return; }
      this.el.innerHTML = moneyStateHtml(rows.map((r) => ({ state: r[cat.key], amount: Number(r[val.key]) || 0 })));
      p.widgetApi.setDisplayState("displayed");
    } catch (err) {
      if (err && err.name === "AbortError") return; // a newer refresh replaced this one
      this.el.innerHTML = '<p class="muted small">Couldn\'t load this widget.</p>';
      p.widgetApi.setDisplayState("displayed");
    }
  }
}

// Pure: one line split by state, then each state with its amount and share.
function moneyStateHtml(rows) {
  const order = ["Disputed", "Waiting", "Unpaid", "Settled"];
  rows = rows.filter((r) => r.amount > 0).sort((a, b) => (order.indexOf(a.state) + 99) % 99 - (order.indexOf(b.state) + 99) % 99);
  const total = rows.reduce((s, r) => s + r.amount, 0) || 1;
  const fmt = (n) => n.toFixed(2);
  const color = (s) => `var(--${{ settled: "settled", held: "held", disputed: "disputed" }[STATE_TONE[s]] || "none"})`;
  return `<div class="ms-total">Net amount<strong class="num">${esc(fmt(total))}</strong></div>
    <div class="ms-line" role="img" aria-label="${esc(rows.map((r) => `${r.state} ${Math.round((r.amount / total) * 100)}%`).join(", "))}">
      ${rows.map((r) => `<span style="flex:${r.amount};background:${color(r.state)}"></span>`).join("")}</div>
    <ul class="ms-list">${rows.map((r) => `<li><span class="cl-mark" data-tone="${esc(STATE_TONE[r.state] || "none")}"></span>
      <span>${esc(r.state)}</span><span class="amt num">${esc(fmt(r.amount))}</span>
      <span class="pct num">${Math.round((r.amount / total) * 100)}%</span></li>`).join("")}</ul>`;
}

// Pure: the same report with every widget full width, one under another, for phones. Shown
// only, never saved (phones are view-only).
function stackForPhone(state) {
  return { ...state, pages: state.pages.map((page) => {
    const ids = Object.keys(page.widgetLayout || {}).sort((a, b) =>
      page.widgetLayout[a].yTrack - page.widgetLayout[b].yTrack || page.widgetLayout[a].xTrack - page.widgetLayout[b].xTrack);
    let y = 0;
    const widgetLayout = {};
    for (const id of ids) {
      const ySpan = Math.max(page.widgetLayout[id].ySpan, 6);
      widgetLayout[id] = { xTrack: 0, yTrack: y, xSpan: 24, ySpan };
      y += ySpan;
    }
    return { ...page, widgetLayout };
  }) };
}

// ---- Theme from our tokens (charts draw on canvas, so colours are resolved values) -----------

const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const isDark = () => document.documentElement.dataset.theme === "dark"
  || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
function studioTheme() {
  return agStudio.studioTheme.withParams({
    fontFamily: FONT, chartFontFamily: FONT, browserColorScheme: isDark() ? "dark" : "light",
    backgroundColor: token("--surface"), foregroundColor: token("--ink"), textColor: token("--ink"),
    accentColor: token("--settled"), borderColor: token("--border"),
    chartTextColor: token("--ink"), chartSubtleTextColor: token("--muted"), chartGridLineColor: token("--border"),
    chartAxisLineColor: token("--border-strong"), chartTooltipBackgroundColor: token("--surface"), chartTooltipTextColor: token("--ink"),
    // Series colours follow the status colours: settled, waiting, disputed, unpaid, then neutrals.
    chartPaletteFills1Color: token("--settled"), chartPaletteFills2Color: token("--held"),
    chartPaletteFills3Color: token("--disputed"), chartPaletteFills4Color: token("--none"),
    chartPaletteFills5Color: token("--ink"), chartPaletteFills6Color: token("--muted"),
  });
}

// ---- The assistant: AG Studio's agent framework on Clearline's own AI ---------------------
// One analyst agent. Each model turn goes to POST /api/insights/ai, which PII-strips it and
// asks Gemini (or Groq for that request only). The tools only read the report and arrange
// widgets: nothing here can move money, answer a dispute or contact PayPal.

const PROMPT_STARTERS = [
  { label: "Needs me this week", prompt: "Which orders and invoices need me this week, and what is the next action for each?" },
  { label: "Where money waits", prompt: "Where is most money waiting or disputed right now? Answer with amounts." },
  { label: "Chart disputes", prompt: "Add a bar chart of disputes by reason." },
];

// Pure: AG-UI events for one turn's reply (text first, then any tool calls).
function turnEvents(reply) {
  const events = [];
  if (reply.text) {
    const messageId = `${reply.id}-text`;
    events.push({ type: "TEXT_MESSAGE_START", messageId, role: "assistant" },
                { type: "TEXT_MESSAGE_CONTENT", messageId, delta: reply.text },
                { type: "TEXT_MESSAGE_END", messageId });
  }
  for (const c of reply.tool_calls || []) {
    events.push({ type: "TOOL_CALL_START", toolCallId: c.id, toolCallName: c.name },
                { type: "TOOL_CALL_ARGS", toolCallId: c.id, delta: c.arguments || "{}" },
                { type: "TOOL_CALL_END", toolCallId: c.id });
  }
  return events;
}

// Pure: the consolidated AgLlmResponse for the same reply.
function turnResponse(reply) {
  const output = [];
  if (reply.text) output.push({ id: `${reply.id}-text`, kind: "output", type: "message", role: "assistant", status: "completed",
                                content: [{ type: "text", text: reply.text, annotations: [] }] });
  for (const c of reply.tool_calls || []) output.push({ id: c.id, kind: "output", type: "function_call", callId: c.id,
                                                        name: c.name, arguments: c.arguments || "{}", status: "completed" });
  return { id: reply.id, createdAt: Date.now(), status: "completed", model: reply.model || undefined, output };
}

// Pure: the request body for one turn (only what the server needs).
function turnBody(request) {
  return { instructions: request.instructions || null, items: request.input || [],
           tools: (request.tools || []).map((t) => ({ name: t.name, description: t.description, parameters: t.parameters })),
           tool_choice: typeof request.toolChoice === "string" ? request.toolChoice : null };
}

const insightsAdapter = {
  executeTurn(request, options = {}) {
    let resolve, reject;
    const complete = new Promise((ok, fail) => { resolve = ok; reject = fail; });
    complete.catch(() => {}); // observed even if the stream is abandoned
    async function* run() {
      try {
        const res = await fetch("/api/insights/ai", { method: "POST", headers: { "Content-Type": "application/json" },
                                                     body: JSON.stringify(turnBody(request)), signal: options.signal });
        const reply = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(reply.detail || "The assistant couldn't answer just now.");
        yield* turnEvents(reply);
        resolve(turnResponse(reply));
      } catch (err) { reject(err instanceof Error ? err : new Error(String(err))); }
    }
    const it = run();
    return { stream: { [Symbol.asyncIterator]: () => it }, complete };
  },
};

// Pure: which widget configure_widget is bound to this round: the newest widget with no data
// yet (the one just added), otherwise the one the seller has selected.
function configureTarget(state) {
  const page = (state.pages || []).find((p) => p.id === state.selectedPageId) || (state.pages || [])[0];
  if (!page) return null;
  const widgets = page.widgets || {};
  const empty = Object.keys(widgets).filter((id) => !Object.values(widgets[id].dataMapping || {}).some((v) => (v || []).length));
  const id = empty.length ? empty[empty.length - 1] : page.selection && page.selection.type === "widget" ? page.selection.id : null;
  return id && widgets[id] ? { widgetId: id, widgetType: widgets[id].type } : null;
}

function analyst(api) {
  return agStudio.createAiHarness(api, ({ tools: { studio } }) => ({
    agents: [agStudio.directLlmRunner({
      id: "analyst", description: "Answers questions about the seller's money and arranges the report.",
      maxTurns: 8, adapter: insightsAdapter, // the server keeps plain questions to 6 and widget building to 8
      instructions: () => "Answer from the report's data. Amounts are in the report's currency. Keep answers short.",
      tools: () => {
        const target = configureTarget(api.getState());
        return [studio.viewSchema(), studio.viewReport(), studio.viewPage(), studio.viewWidget(), studio.executeQuery(),
                studio.addWidget(), studio.positionWidget(), ...(target ? [studio.configureWidget(target)] : [])];
      },
    })],
    primary: "analyst",
    promptStarters: PROMPT_STARTERS,
  }));
}

// ---- Page ----------------------------------------------------------------------------------

let api = null, saveTimer = 0, applying = false, currency = "USD";
let report = null; // the seller's real (desktop) layout; phones show a stacked copy of it
let savedLayout = null; // layoutKey() of the report as loaded, once the seller first touches it
const desktop = () => window.innerWidth >= 1024;
const $ = (id) => document.getElementById(id);
const rawFetch = globalThis.fetch;
globalThis.fetch = async (...args) => {
  const res = await rawFetch(...args);
  if (res.status === 401) location.href = "/login";
  return res;
};

function showFailure() {
  $("studio").classList.add("failed");
  $("studio").innerHTML = `<div class="empty studio-failed"><strong>Insights couldn't load</strong>
    The main dashboard still works. <a href="/dashboard">Open the dashboard</a></div>`;
  $("mode").hidden = true; $("reset").hidden = true; document.querySelector(".phone-note").hidden = true;
}

function setSaved(text) { $("saved").textContent = text; }

// Pure: the part of Studio's state that is the seller's layout. Which side panels are open isn't:
// entering Edit layout moves them, and that alone must not save anything.
function layoutKey(state) {
  const { panels, ...layout } = state || {};
  return JSON.stringify(layout);
}

// Pure: is `state` a real change from the layout in `saved`?
const layoutChanged = (state, saved) => saved !== null && layoutKey(state) !== saved;

// Debounced save of every layout change; the server keeps it per seller.
function queueSave(state) {
  if (api && !applying) roomForAssistant(state);
  if (applying || !desktop()) return; // phones only view a stacked copy: nothing to save
  report = state;
  if (!layoutChanged(state, savedLayout)) return;
  clearTimeout(saveTimer);
  setSaved("Saving…");
  const key = layoutKey(state);
  saveTimer = setTimeout(async () => {
    try {
      const res = await fetch("/api/insights/layout", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state) });
      if (res.ok) savedLayout = key;
      setSaved(res.ok ? `Saved ${UI.dateTime(Date.now() / 1000)}` : "Couldn't save this layout");
    } catch { setSaved("Couldn't save this layout"); }
  }, 1200);
}

function setMode(edit) {
  api.setProperty("mode", edit ? "edit" : "view");
  $("mode").textContent = edit ? "Done editing" : "Edit layout";
  $("mode").setAttribute("aria-pressed", String(edit));
  if (edit) roomForAssistant(api.getState(), true);
}

// Pure: when the AI Assistant panel is open, Compose, Data and Filters are collapsed so the
// report keeps its width (the seller can reopen any of them). null = the assistant is closed.
function panelsForAssistant(panels) {
  panels = panels || {};
  if (panels.ai && panels.ai.collapsed) return null;
  const closed = ["edit", "data", "filters"]; // Studio's ids: edit is the Compose panel
  return { ...panels, ...Object.fromEntries(closed.map((k) => [k, { collapsed: true }])) };
}

// On entering Edit layout, and whenever the assistant is opened. Studio doesn't re-apply
// saved panel flags on a mode switch, so they're set again (a new object) every time.
let assistantWasOpen = false;
function roomForAssistant(state, force = false) {
  const open = !(state.panels && state.panels.ai && state.panels.ai.collapsed);
  const opened = open && (force || !assistantWasOpen);
  assistantWasOpen = open;
  if (!opened || api.getProperty("mode") !== "edit") return;
  const panels = panelsForAssistant(state.panels);
  if (panels) api.setState({ ...state, panels });
}

// Pure: a step's "1 issue" pill is Studio's health advice about a step that worked. A step that
// failed says so itself, so its pill stays. Row counts ("3 rows") are never advisories.
const isAdvisory = (pill, failed) => !failed && /^\d+ issues?$/.test(pill.trim());

// Studio has no option for this, so the chat is filtered as it renders.
function hideAdvisories() {
  const failed = (el) => el.closest(":has(> .ag-studio-ai-step-row)")?.querySelector(":scope > .ag-studio-ai-step-row-failed");
  document.querySelectorAll(".ag-studio-ai-step-pill").forEach((pill) => {
    pill.style.display = isAdvisory(pill.textContent, failed(pill)) ? "none" : "";
  });
  document.querySelectorAll(".ag-studio-ai-step-detail-issues").forEach((notes) => {
    notes.style.display = failed(notes) ? "" : "none";
  });
}

function watchAdvisories() {
  new MutationObserver(hideAdvisories).observe($("studio"), { childList: true, subtree: true, characterData: true });
}

async function start() {
  if (typeof agStudio === "undefined") { showFailure(); return; }
  try {
    const [licence, layout, data] = await Promise.all([
      fetch("/api/insights/license").then((r) => r.json()),
      fetch("/api/insights/layout").then((r) => r.json()),
      fetch("/api/dashboard").then((r) => r.json()),
    ]);
    if (licence.key) agStudio.AgStudioLicenseManager.setLicenseKey(licence.key); // no key: AG Studio shows its watermark
    agStudio.AgStudioModuleRegistry.registerModules([agStudio.AgStudioAiModule]);
    // Studio's own widgets (charts, grids, values, filters) plus ours, appended to its menu:
    // replacing the menu would leave sellers and the assistant with Money state only.
    const widgets = (config) => agStudio.createWidgets({
      additionalTypes: [{
        id: "money-state", label: "Money state", icon: { url: "data:image/svg+xml;utf8," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect x="2" y="10" width="9" height="4" rx="2" fill="#0E8C7E"/><rect x="12" y="10" width="5" height="4" rx="2" fill="#B7791F"/><rect x="18" y="10" width="4" height="4" rx="2" fill="#C2364A"/></svg>') },
        dataMapping: {
          state: { type: "field", supportedRoles: ["category"], requires: { cardinality: "one" }, required: true },
          value: { type: "field", supportedRoles: ["numeric"], requires: { cardinality: "one" }, required: true },
        },
        form: (fp) => fp.createDefaults({ dataMappingItems: [{ key: "state", label: "Money state" }, { key: "value", label: "Amount" }] }),
        comp: MoneyStateWidget,
        defaultSize: { width: 420, height: 320 }, minSize: { width: 240, height: 200 },
        ai: { description: "Clearline's money state: one line split into settled, waiting, disputed and unpaid amounts, in Clearline's status colours, with each state's amount and share." },
      }],
      menu: [...config.menu, { label: "Clearline", widgetIds: ["money-state"] }],
    });
    report = layout.state || defaultState(data.charts.currency);
    $("studio").innerHTML = "";
    api = agStudio.createStudio($("studio"), {
      data: { sources: toSources(data), formats: agStudio.createFormats({ overrides: { currencyFormat: { formatOptions: MONEY }, decimalFormat: { formatOptions: MONEY } } }) }, widgets, mode: "view", theme: studioTheme(),
      initialState: desktop() ? report : stackForPhone(report),
      layout: { minWidth: desktop() ? 460 : 300 },
      onStateUpdated: (e) => queueSave(e.state),
      ai: ({ api: studioApi }) => analyst(studioApi),
    });
    currency = data.charts.currency;
    // Studio applies initialState a moment after this returns, without an update event, so
    // "the layout as loaded" can only be read once it has: at the seller's first touch.
    const loaded = () => { savedLayout ??= layoutKey(api.getState()); };
    for (const [el, kind] of [[$("studio"), "pointerdown"], [$("studio"), "keydown"], [$("mode"), "pointerdown"]])
      el.addEventListener(kind, loaded, { capture: true, once: true });
    watchAdvisories();
    if (layout.updated_at) setSaved(`Your layout, saved ${UI.dateTime(layout.updated_at)}`);
  } catch (err) { showFailure(); return; }

  $("mode").addEventListener("click", () => setMode(api.getProperty("mode") !== "edit"));
  $("reset").addEventListener("click", async () => {
    if (!await UI.confirm({ title: "Reset Insights to the default layout?", confirm: "Reset layout",
      what: ["Your arranged widgets are replaced by Clearline's default report.", "No money or orders are affected."] })) return;
    await fetch("/api/insights/layout", { method: "DELETE" });
    report = defaultState(currency);
    applying = true; api.setState(desktop() ? report : stackForPhone(report)); applying = false;
    savedLayout = layoutKey(api.getState());
    setSaved("Default layout");
  });
  // Phones: view only, stacked. Crossing the breakpoint swaps between the two views.
  let wasDesktop = desktop();
  window.addEventListener("resize", () => {
    if (desktop() === wasDesktop) return;
    wasDesktop = desktop();
    if (!wasDesktop) setMode(false);
    applying = true; api.setState(wasDesktop ? report : stackForPhone(report)); applying = false;
    api.setProperty("layout", { minWidth: wasDesktop ? 460 : 300 });
  });
  // Charts draw on canvas, so the theme is rebuilt from the tokens when it changes.
  const retheme = () => api.setProperty("theme", studioTheme());
  new MutationObserver(retheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", retheme);
}

if (typeof document !== "undefined" && document.readyState !== undefined) start();
