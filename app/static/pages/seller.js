const CHEV = '<svg class="chev" width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
let busy = 0; // requests in flight from our buttons; auto-refresh waits for them
let ENV = null; // "sandbox" or "live", from /api/config; sandbox-only tools need "sandbox"
let demoAccount = false; // the judges' shared account: its own settings are locked
const openOrders = new Set(); // expanded rows survive the 10-second refresh
const openExports = new Set(); // and so does an opened export paperwork panel
// "Open on Orders" from the dashboard: /seller#order-38815W (the masked id's last 6 characters).
let deepLink = ((location.hash || "").match(/^#order-([\w-]{4,})$/) || [])[1] || null;

// Session expired or logged out elsewhere: any 401 from a seller API goes to the login page.
const rawFetch = globalThis.fetch;
globalThis.fetch = async (...args) => {
  const res = await rawFetch(...args);
  if (res.status === 401) location.href = "/login";
  return res;
};

// (Errors, the clear line and the latest dispute state live in /static/history.js.)

// ---- Disputes ------------------------------------------------------------------------------

function daysLeft(due) {
  const date = new Date(due);
  if (!due || isNaN(date)) return null;
  return { date, days: Math.ceil((date - Date.now()) / 86400000) };
}
const leftText = (days) => (days > 1 ? `${days} days left` : days === 1 ? "1 day left" : days === 0 ? "Due today" : "Overdue");
function respondBy(due) {
  const d = daysLeft(due);
  if (!d) return "";
  return `<dt>Respond by</dt><dd class="due">${esc(UI.date(d.date / 1000))}, ${esc(leftText(d.days).toLowerCase())}</dd>`;
}


// Sandbox-only test tool for disputes PayPal is reviewing. Lives in the dispute panel, so it
// doesn't depend on the AI card (UNDER_REVIEW disputes often have no recommendation, or
// still allow a Refund, which shows the full card). Never rendered in live mode.
function sandboxTool(state) {
  if (ENV !== "sandbox" || state?.payload.status !== "UNDER_REVIEW") return "";
  return `<div class="sandbox-tool actions"><button class="sandbox small" data-sandbox="${esc(state.payload.dispute_id)}" title="Test tool: only exists in the PayPal Sandbox">Sandbox: simulate PayPal asking for your evidence</button><div class="msg"></div></div>`;
}

function disputePanel(state) {
  if (!state) return { badge: "", panel: "", disputed: false, resolved: false };
  const p = state.payload;
  const resolved = isResolved(state);
  const badge = resolved ? '<span class="pill">Dispute closed</span>'
    : `<span class="pill disputed">${esc(UI.word("status", p.status) || "Disputed")}</span>`;
  const panel = `<div class="dispute${resolved ? " resolved" : ""}">
      <h3>${resolved ? "Dispute (closed)" : esc(disputeName(p))}</h3>
      <dl class="kv">
        <dt>Reason</dt><dd>${esc(reasonWords(p.reason))}</dd>
        <dt>Amount</dt><dd class="num">${esc(money(p.dispute_amount))}</dd>
        <dt>Status</dt><dd>${esc(UI.word("status", p.status) || "Unknown")}</dd>
        ${resolved ? "" : respondBy(p.seller_response_due_date)}
        <dt>PayPal case</dt><dd class="mono">${esc(p.dispute_id)}</dd>
      </dl>
      ${p.enrichment_error ? '<div class="muted small">Could not load full dispute details from PayPal.</div>' : ""}
      ${sandboxTool(state)}
    </div>`;
  return { badge, panel, disputed: !resolved, resolved };
}

// The server stores which decisions PayPal currently allows with every dispute snapshot
// (same rules it checks before each step). Older rows lack it: let the server decide.
const actionAllowed = (decision, p) => !p.allowed_decisions || p.allowed_decisions.includes(decision);

// One line per Approve click: "Fight: Evidence sent, Message skipped (dispute is waiting for the buyer)".
function actionRuns(actions) {
  const runs = [];
  for (const e of actions) {
    const key = e.payload.run_id || `${e.created_at}`;
    let run = runs.find((r) => r.key === key);
    if (!run) runs.push(run = { key, at: e.created_at, action: e.payload.action, steps: [] });
    run.steps.push(e);
  }
  const step = (e) => {
    const name = esc(STEP_NAMES[e.payload.endpoint] || UI.sentence(e.payload.endpoint));
    if (e.kind === "dispute_action_sent") return `<span class="ok">${name} sent</span>`;
    if (e.kind === "dispute_action_skipped") return `<span class="muted">${name} skipped (${esc(e.payload.reason)})</span>${techHtml(errorParts(e.payload).tech)}`;
    return `<span class="bad">${name} failed (${errorHtml(e.payload)})</span>`;
  };
  return runs.map((r) => `<li>${esc(DECISIONS[r.action] || UI.sentence(r.action))}: ${r.steps.map(step).join(", ")} <span class="muted small">${fmt(r.at)}</span></li>`).join("");
}

// Why the seller can't act right now (null = some decision is allowed).
function noActionReason(p, resolved) {
  if (resolved) return "Dispute closed";
  if (p.status === "WAITING_FOR_BUYER_RESPONSE") return "Waiting for the buyer, nothing to send right now";
  if (p.allowed_decisions && !p.allowed_decisions.length)
    return p.status === "UNDER_REVIEW" ? "PayPal is reviewing this case" : "PayPal allows no seller action right now";
  return null;
}

// What Approve does, in the button's own words.
function approveLabel(decision, r, p, docs) {
  if (decision === "REFUND") return `Refund ${money(p.dispute_amount)}`;
  if (decision === "OFFER") return `Offer ${UI.money(r.offer?.amount, p.dispute_amount?.currency_code)}`;
  return docs.length ? "Send document and note" : "Send evidence";
}

const AI_TAG = '<span class="tag"><svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3l2 5.5L19.5 10 14 12l-2 6-2-6-5.5-2L10 8.5z"/></svg>AI suggestion, you decide</span>';

function aiCard(events, state, resolved) {
  if (!state) return "";
  const id = state.payload.dispute_id;
  const mine = events.filter((e) => e.payload.dispute_id === id);
  const ai = mine.filter((e) => e.kind === "ai_recommendation" || e.kind === "ai_failed").pop();
  const actions = mine.filter((e) => ["dispute_action_sent", "dispute_action_failed", "dispute_action_skipped"].includes(e.kind));
  const results = actions.length ? `<div class="sent"><h4>Sent to PayPal</h4><ul>${actionRuns(actions)}</ul></div>` : "";
  const regen = resolved ? "" : `<button class="secondary" data-regen="${esc(id)}">Ask AI again</button>`;

  // Nothing to approve: one line instead of editable drafts.
  const blocked = noActionReason(state.payload, resolved);
  if (blocked) {
    const r = ai?.kind === "ai_recommendation" ? ai.payload.recommendation : null;
    const last = r ? `Last AI suggestion: ${DECISIONS[r.decision]}, ${Math.round(r.confidence * 100)}% confident.` : "No AI suggestion.";
    return `<div class="ai compact"><span>${esc(last)} ${esc(blocked)}.</span></div>`;
  }

  if (!ai) return `<div class="ai"><div class="ai-head">${AI_TAG}</div><div class="muted">No suggestion yet.</div>${results}<div class="actions">${regen}</div><div class="msg"></div></div>`;
  if (ai.kind === "ai_failed") return `<div class="ai"><div class="ai-head">${AI_TAG}</div>
      <div class="bad">The AI couldn't make a suggestion. Ask again in a moment.</div><div class="muted small">${errorHtml(ai.payload)}</div>
      ${results}<div class="actions">${regen}</div><div class="msg"></div></div>`;

  const p = ai.payload, r = p.recommendation;
  const model = p.provider === "groq" ? `Groq ${p.model} (fallback)` : `Gemini ${p.model}`;
  const facts = (keys) => (keys.length ? `Based on: ${keys.map((k) => k.replace(/[._]+/g, " ")).join(", ")}` : "No facts cited");
  const reasons = r.reasons.map((x) => `<li>${esc(x.text)} <span class="based">${esc(facts(x.fact_keys))}</span></li>`).join("");
  const missing = r.missing_evidence.length ? `<h4>Missing evidence</h4><ul>${r.missing_evidence.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : "";
  const cur = state.payload.dispute_amount?.currency_code || "";
  const offer = r.offer ? `<div class="actions">
      <label class="field">Offer type <input data-field="offer_type" value="${esc(r.offer.type)}" size="22" /></label>
      <label class="field">Amount (${esc(cur)}) <input data-field="offer_amount" type="number" step="0.01" min="0.01" value="${esc(r.offer.amount)}" /></label></div>` : "";
  // Claim stage: PayPal needs a document the seller provides; Approve waits for the file.
  const docs = r.decision === "FIGHT" ? (state.payload.document_evidence || []) : [];
  const picker = docs.length ? `<label class="field">Evidence document (PayPal needs one for this claim)
      <select data-field="evidence_type">${docs.map((d) =>
        `<option value="${esc(d.type)}"${d.type === "PROOF_OF_DELIVERY_SIGNATURE" ? " selected" : ""}>${esc(d.label)}</option>`).join("")}</select></label>
      <input type="file" data-field="evidence_file" accept=".pdf,.jpg,.jpeg,.png,.gif,application/pdf,image/jpeg,image/png,image/gif" aria-label="Evidence file" />
      <div class="hint">One PDF, JPG, PNG or GIF, at most 5 MB. Clearline only uploads files you provide; it never generates proof.</div>` : "";
  const approve = actionAllowed(r.decision, state.payload)
      ? `<button data-approve="${esc(id)}" data-decision="${esc(r.decision)}"${docs.length ? ' data-needs-file="1" disabled title="Attach a document first"' : ""} class="${r.decision === "FIGHT" ? "" : "money"}">${esc(approveLabel(r.decision, r, state.payload, docs))}</button>`
      : `<span class="muted">PayPal doesn't currently allow “${esc(DECISIONS[r.decision])}” on this dispute.</span>`;
  return `<div class="ai" data-dispute="${esc(id)}">
      <div class="ai-head">${AI_TAG}<span class="meter">${Math.round(r.confidence * 100)}% confident</span></div>
      <h4>${esc({ FIGHT: "Fight it with your evidence", REFUND: "Refund the buyer", OFFER: "Offer the buyer a partial refund" }[r.decision] || DECISIONS[r.decision])}</h4>
      <ul>${reasons}</ul>${missing}${offer}
      <label class="field">Message to the buyer <textarea data-field="message_to_buyer">${esc(r.message_to_buyer)}</textarea></label>
      <label class="field">Note to PayPal <textarea data-field="note_to_paypal">${esc(r.note_to_paypal)}</textarea></label>
      ${picker}
      ${results}
      <div class="actions">${approve}${regen}</div><div class="msg"></div>
      <div class="fine">Nothing is sent to PayPal until you click ${esc(approveLabel(r.decision, r, state.payload, docs))}.</div>
      <details class="about"><summary>About this suggestion</summary><dl class="kv">
        <dt>Model</dt><dd>${esc(model)}</dd><dt>Took</dt><dd class="num">${(p.latency_ms / 1000).toFixed(1)} s</dd></dl></details>
    </div>`;
}

// ---- The buyer's side ------------------------------------------------------------------------

// Their private link, their requests (with a reply box) and, for refund requests, the
// refund buttons. Only the seller's click (and the confirm dialog) sends a refund.
function buyerSection(orderId, b) {
  if (!b) return "";
  const link = new URL(b.link, location.origin).href;
  const canRefund = Number(b.refundable) > 0;
  const requests = b.requests.map((r) => `<div class="req">
      <div class="head"><strong>${esc(BUYER_TYPES[r.type] || r.type)}</strong>
        ${r.via === "agent" ? '<span class="pill plain">via AI agent</span>' : ""}
        ${r.auto_refund ? `<span class="pill settled">Auto-resolved: refunded ${esc(r.auto_refund.amount)} ${esc(r.auto_refund.currency)}</span>` : ""}
        <span class="muted small">${fmt(r.at)}</span></div>
      ${r.reason || r.amount ? `<dl class="kv small">${r.reason ? `<dt>Reason</dt><dd>${esc(r.reason)}</dd>` : ""}${r.amount ? `<dt>Asks for</dt><dd class="num">${esc(r.amount)} ${esc(b.currency)}</dd>` : ""}</dl>` : ""}
      ${r.auto_refund ? `<div class="muted small">Rule: ${esc(r.auto_refund.rule)}</div>` : ""}
      ${r.message ? `<div class="said">${esc(r.message)}</div>` : '<div class="muted said">No message.</div>'}
      ${r.replies.map((x) => `<div class="reply"><div class="by">${x.auto ? "Automatic reply" : "You"}</div>${esc(x.message)}</div>`).join("")}
      <textarea data-reply-text placeholder="Reply to the buyer (shown on their order page)" maxlength="1000" aria-label="Reply to the buyer"></textarea>
      <div class="actions">
        <button class="secondary" data-reply="${esc(r.id)}" data-order="${esc(orderId)}">Send reply</button>
        ${canRefund ? `
          <button data-refund="full" data-request="${esc(r.id)}" data-order="${esc(orderId)}" data-max="${esc(b.refundable)}" data-currency="${esc(b.currency)}" class="money">Refund ${esc(b.refundable)} ${esc(b.currency)}</button>
          <button data-refund="partial" data-request="${esc(r.id)}" data-order="${esc(orderId)}" data-max="${esc(b.refundable)}" data-currency="${esc(b.currency)}" class="secondary">Refund part of it</button>` : ""}
      </div><div class="msg"></div></div>`).join("");
  const refunds = b.refunds.map((x) => `<div class="refunded">Refund of ${esc(x.amount)} ${esc(x.currency)} sent${x.status && x.status !== "COMPLETED" ? ` (${esc(UI.word("payment", x.status))})` : ""}, ${fmt(x.at)}</div>`).join("");
  return `<div class="buyer">
      <div class="linkrow"><span class="muted">Buyer page</span>
        <a href="${esc(link)}" target="_blank" rel="noopener" class="mono">${esc(b.link.slice(0, 14))}…</a>
        <button class="ghost small" data-copy="${esc(link)}">Copy link</button></div>
      ${refunds}
      ${b.requests.length ? `<h3>Buyer requests</h3>${requests}` : ""}
    </div>`;
}

function showStats(s) {
  const el = document.getElementById("stats");
  el.innerHTML = `<strong>${esc(s.questions_answered_before_dispute)}</strong> questions answered before a dispute${
    s.auto_resolved ? `, ${esc(s.auto_resolved)} by your policy` : ""}`;
  el.hidden = false;
}

// India export paperwork for a received international payment. Guidance only: the AI may
// suggest a purpose code, but only the seller's pick is recorded.
function exportSection(orderId, x) {
  if (!x) return "";
  const s = x.suggestion;
  const options = Object.entries(x.purpose_codes).map(([code, label]) =>
    `<option value="${esc(code)}"${code === x.purpose_code ? " selected" : ""}>${esc(label)} (${esc(code)})</option>`).join("");
  const line = [x.purpose_code ? `Purpose code ${x.purpose_code}` : "Purpose code not chosen",
                x.fira_downloaded ? "FIRA downloaded" : "FIRA not downloaded yet"].join(", ");
  return `<details class="export" data-export="${esc(orderId)}"${openExports.has(orderId) ? " open" : ""}>
      <summary><span class="ex-title">Export paperwork</span><span class="ex-line ${x.purpose_code && x.fira_downloaded ? "ok" : "muted"}">${esc(line)}</span>${CHEV}</summary>
      <div class="ex-body">
      <div class="muted small">${esc(x.disclaimer)}</div>
      <dl class="kv"><dt>Payment</dt><dd><span class="num">${esc(x.payment.amount)} ${esc(x.payment.currency)}</span> from ${esc(x.payment.country || "abroad")}</dd>
        <dt>For</dt><dd>${esc(x.payment.description)}</dd>
        <dt>Purpose code</dt><dd>${x.purpose_code ? `<span class="ok">${esc(x.purpose_code)}</span>` : '<span class="bad">Not chosen</span>'}</dd></dl>
      <div class="actions"><select data-purpose aria-label="Purpose code"><option value="">Choose the code that fits this payment</option>${options}</select>
        <button class="secondary" data-purpose-save="${esc(orderId)}">Save</button>
        <button class="secondary" data-purpose-suggest="${esc(orderId)}">Ask AI to suggest</button></div>
      ${s ? `<div class="muted small">AI suggests <strong>${esc(s.code)}</strong>: ${esc(s.reason)} (not applied; pick it above if it fits)</div>` : ""}
      <div class="muted small">PayPal applies the purpose code set on your account (Settings, then Purpose code) to your payments; check it matches.</div>
      <label class="check"><input type="checkbox" data-fira="${esc(orderId)}"${x.fira_downloaded ? " checked" : ""} /> FIRA downloaded</label>
      <div class="muted small">PayPal's monthly digital FIRA is under Reports, Statements, FIRA, by the 15th of next month.</div>
      <div class="muted small">Sources: ${x.sources.map((src) => `<a href="${esc(src.url)}" target="_blank" rel="noopener">${esc(src.label)}</a>`).join(", ")}</div>
      <div class="msg"></div></div></details>`;
}

// ---- Invoices ------------------------------------------------------------------------------

const INVOICE_STATUS_CLASS = { Paid: "settled", Overdue: "disputed", Sent: "held", Cancelled: "", Draft: "" };
let invoiceRows = [];

function renderInvoiceList(rows) {
  const list = document.getElementById("invoice-list");
  list.innerHTML = rows.length ? `<table class="tbl wide"><thead><tr><th>Number</th><th>Client</th><th class="num">Amount</th><th>Due</th><th>Status</th><th><span class="visually-hidden">Actions</span></th></tr></thead><tbody>${
    rows.map((r) => `<tr data-invoice="${esc(r.invoice_id)}">
      <td data-label="Number">${esc(r.number || "None")}</td><td data-label="Client">${esc(r.client || "None")}</td>
      <td data-label="Amount" class="num">${esc(r.amount)} ${esc(r.currency)}</td>
      <td data-label="Due">${esc(r.due_date ? UI.isoDate(r.due_date) : "No due date")}</td>
      <td data-label="Status"><span class="pill ${INVOICE_STATUS_CLASS[r.display_status] || ""}">${esc(r.display_status)}${r.days_overdue ? `, ${esc(r.days_overdue)} days` : ""}</span></td>
      <td data-label="">${r.display_status === "Draft" ? `<button class="small" data-invoice-send="${esc(r.invoice_id)}">Send invoice</button>` : ""}
          ${r.display_status === "Overdue" ? `<button class="secondary small" data-invoice-remind="${esc(r.invoice_id)}">Draft reminder</button>` : ""}</td></tr>
      <tr class="reminder-row" hidden><td colspan="6" class="wide" data-reminder="${esc(r.invoice_id)}"></td></tr>`).join("")}</tbody></table>`
    : '<div class="empty"><strong>No invoices yet</strong>Describe one above and the AI drafts it for you.</div>';
  list.querySelectorAll("[data-invoice-send]").forEach((b) => b.addEventListener("click", () => sendInvoice(b)));
  list.querySelectorAll("[data-invoice-remind]").forEach((b) => b.addEventListener("click", () => draftReminder(b)));
}

async function renderInvoices() {
  const res = await fetch("/api/invoices");
  if (!res.ok) return;
  invoiceRows = await res.json();
  renderInvoiceList(invoiceRows);
}

// ---- Today: what needs the seller, each with its one next action ---------------------------

// A paid order with no tracking yet can ship, unless all its money has gone back: a partial
// refund leaves something to send. `buyer` holds what's refundable (null if it didn't load).
function canShip(events, buyer) {
  const kinds = new Set(events.map((e) => e.kind));
  if (!kinds.has("payment_captured") || kinds.has("invoice_paid") || kinds.has("tracking_added")) return false;
  return !buyer || Number(buyer.refundable) > 0;
}

function attentionItems(orders, invoices) {
  const items = [];
  for (const o of orders.filter((x) => !x.archived)) {
    const state = currentDispute(o.events);
    if (state && !disputePanel(state).resolved && !noActionReason(state.payload, false)) {
      const p = state.payload, d = daysLeft(p.seller_response_due_date);
      items.push({ rank: 0, sort: d ? d.days : 99, tone: "disputed", title: `Respond to the ${disputeName(p).toLowerCase()}: ${reasonWords(p.reason)}`,
        sub: `Order …${o.order_id.slice(-6)}`, when: d ? leftText(d.days) : "", urgent: d && d.days <= 3,
        amount: money(p.dispute_amount), action: "Review and respond", target: o.order_id, focus: ".ai" });
    } else if (state && !disputePanel(state).resolved) { // open, but it's not the seller's turn
      const p = state.payload;
      items.push({ rank: 4, sort: 0, tone: "disputed", title: `${disputeName(p)} open: ${reasonWords(p.reason)}`,
        sub: `Order …${o.order_id.slice(-6)}`, when: noActionReason(p, false), amount: money(p.dispute_amount),
        action: "View", target: o.order_id, focus: ".dispute", waiting: true });
    }
    for (const r of openRequests(o.events)) {
      items.push({ rank: 1, sort: r.created_at, tone: "held", title: `Buyer: ${BUYER_TYPES[r.payload.type] || "Sent a request"}`,
        sub: r.payload.message ? `“${r.payload.message.slice(0, 80)}”` : `Order …${o.order_id.slice(-6)}`,
        when: `Asked ${UI.date(r.created_at)}`, action: "Reply", target: o.order_id, focus: "[data-reply-text]" });
    }
    if (canShip(o.events, o.buyer) && !state) {
      const paid = o.events.find((e) => e.kind === "payment_captured");
      items.push({ rank: 3, sort: paid.created_at, tone: "settled", title: "Paid, waiting to ship", sub: `Order …${o.order_id.slice(-6)}`,
        when: `Paid ${UI.date(paid.created_at)}`, amount: o.amount, action: "Add tracking", target: o.order_id, focus: "[name=tracking_number]" });
    }
  }
  for (const r of invoices.filter((x) => x.display_status === "Overdue")) {
    items.push({ rank: 2, sort: -(r.days_overdue || 0), tone: "held", title: `Invoice ${r.number || ""} is overdue`.replace("  ", " "),
      sub: r.client || "", when: `${r.days_overdue} days overdue, due ${UI.isoDate(r.due_date)}`, urgent: true, amount: UI.money(r.amount, r.currency),
      action: "Draft reminder", invoice: r.invoice_id });
  }
  return items.sort((a, b) => a.rank - b.rank || a.sort - b.sort);
}

// Disputes where it's PayPal's or the buyer's turn: below Today, folded away.
let waitingOpen = false;
function waitingHtml(items) {
  if (!items.length) return "";
  return `<details class="panel disclose waiting" id="waiting-group"${waitingOpen ? " open" : ""}><summary><span class="d-title">Waiting on others</span>
      <span class="d-line">${items.length} with PayPal or the buyer, nothing for you to do yet</span>${CHEV}</summary>
      <div class="d-body">${attentionHtml(items, "data-wait")}</div></details>`;
}

function attentionHtml(items, attr = "data-attend") {
  if (!items.length) return '<div class="empty"><strong>Nothing needs you right now</strong>New disputes, buyer questions and overdue invoices show up here.</div>';
  return `<ul class="attention">${items.map((it, i) => `<li>
      <span class="cl-mark" data-tone="${esc(it.tone)}" aria-hidden="true"></span>
      <div class="what"><strong>${esc(it.title)}</strong><span class="muted small">${esc(it.sub)}</span>
        ${it.when ? `<div class="when${it.urgent ? " urgent" : ""}">${esc(it.when)}</div>` : ""}</div>
      ${it.amount ? `<span class="amount num">${esc(it.amount)}</span>` : ""}
      <button class="${i === 0 && attr === "data-attend" ? "" : "secondary"} small" ${attr}="${i}">${esc(it.action)}</button></li>`).join("")}</ul>`;
}

function attend(it) {
  if (it.invoice) {
    const b = document.querySelector(`[data-invoice-remind="${CSS.escape(it.invoice)}"]`);
    b?.scrollIntoView({ block: "center" });
    if (b) draftReminder(b);
    return;
  }
  openOrders.add(it.target);
  const row = document.querySelector(`.order[data-order-id="${CSS.escape(it.target)}"]`);
  if (!row) return;
  setOpen(row, true);
  row.scrollIntoView({ block: "start" });
  const el = row.querySelector(it.focus);
  el?.focus({ preventScroll: true });
}

function setOpen(row, open) {
  const head = row.querySelector(".order-head"), body = row.querySelector(".order-body");
  head.setAttribute("aria-expanded", String(open));
  body.hidden = !open;
  if (open) openOrders.add(row.dataset.orderId); else openOrders.delete(row.dataset.orderId);
  if (open && stale().length) render(false).catch(() => {}); // its full timeline, on first open
}

// ---- Orders --------------------------------------------------------------------------------

const getTimeline = async (id) => (await fetch(`/api/orders/${encodeURIComponent(id)}/timeline`)).json();
const isSide = (id) => id === "other" || id.startsWith("unmatched:");
const CARRIERS = ["DHL", "FEDEX", "UPS", "USPS"];

// The row's pill: the one thing to know about this order.
function rowStatus(events, state, d) {
  const kinds = new Set(events.map((e) => e.kind));
  if (kinds.has("archived")) return '<span class="pill plain">Archived</span>';
  if (d.disputed) return d.badge;
  if (openRequests(events).length) return '<span class="pill held">Buyer is waiting</span>';
  if (orderTone(events, state) === "held") return '<span class="pill held">Payment on hold</span>';
  if (kinds.has("refund_sent")) return '<span class="pill settled">Refunded</span>';
  if (kinds.has("invoice_paid")) return '<span class="pill settled">Paid</span>';
  if (kinds.has("tracking_added")) return '<span class="pill settled">Shipped</span>';
  if (kinds.has("payment_captured")) return '<span class="pill settled">Paid, not shipped</span>';
  if (d.badge) return d.badge;
  return '<span class="pill">Not paid</span>';
}

function orderAmount(events) {
  const inv = events.find((e) => e.kind === "invoice_paid");
  if (inv) return UI.money(inv.payload.amount, inv.payload.currency);
  const c = events.find((e) => e.kind === "order_created");
  return c ? UI.money(c.payload.amount, c.payload.currency) : "";
}

function orderRow(o) {
  const { events } = o; // the summary's: enough for the header, Today and Mark shipped
  const full = details.get(o.order_id); // the rest, once the order has been opened
  const state = currentDispute(events);
  const d = disputePanel(state);
  const invoicePaid = events.find((e) => e.kind === "invoice_paid");
  const tone = orderTone(events, state);
  const open = openOrders.has(o.order_id);
  const title = invoicePaid ? `Invoice ${invoicePaid.payload.number || o.order_id}` : `Order …${o.order_id.slice(-6)}`;
  const sub = invoicePaid ? "Paid by invoice" : `Created ${UI.date(events[0]?.created_at || 0)}`;
  const bodyId = `o-${o.order_id.replace(/[^\w-]/g, "")}`;
  const form = canShip(events, o.buyer) ? `
      <form class="ship" data-order="${esc(o.order_id)}">
        <label class="field">Tracking number <input name="tracking_number" required autocomplete="off" /></label>
        <label class="field">Carrier <select name="carrier">${CARRIERS.map((c) => `<option value="${c}">${esc(UI.word("carrier", c))}</option>`).join("")}</select></label>
        <button>Mark shipped</button>
      </form><div class="msg"></div>` : "";
  return `<article class="order" data-order-id="${esc(o.order_id)}" data-tone="${esc(tone)}">
      <button class="order-head" aria-expanded="${open}" aria-controls="${esc(bodyId)}">
        <span class="cl-mark" data-tone="${esc(tone)}" aria-hidden="true"></span>
        <span class="title"><strong>${esc(title)}</strong><span>${esc(sub)}</span></span>
        ${rowStatus(events, state, d)}
        <span class="amount num">${esc(orderAmount(events))}</span>${CHEV}
      </button>
      <div class="order-body" id="${esc(bodyId)}"${open ? "" : " hidden"}>${full ? `
        <div class="history"><h3>History</h3>${timelineRows(full.events, o.order_id)}</div>
        <div class="work">${d.panel}${aiCard(full.events, currentDispute(full.events), d.resolved)}${form}${buyerSection(o.order_id, full.buyer)}${exportSection(o.order_id, full.exp)}</div>`
        : '<div class="muted small">Loading…</div>'}
      </div></article>`;
}

// ---- Loading: one summary per refresh. Unchanged, it's a 304 with no body. ----------------

let summary = null, summaryTag = null;
const details = new Map(); // an opened order's full timeline, buyer view and export: { last_id, events, buyer, exp }

async function loadSummary(force) {
  const url = $("show-archived")?.checked ? "/api/seller/summary?include_archived=true" : "/api/seller/summary";
  const res = await fetch(url, { cache: "no-store", headers: !force && summaryTag ? { "If-None-Match": summaryTag } : {} });
  if (res.status === 304 || !res.ok) return false;
  summaryTag = res.headers.get("ETag");
  summary = await res.json();
  return true;
}

async function loadDetails(o) {
  const id = encodeURIComponent(o.order_id);
  const [events, buyer, exp] = await Promise.all([
    getTimeline(o.order_id),
    o.buyer ? fetch(`/api/orders/${id}/buyer`).then((r) => (r.ok ? r.json() : null)).catch(() => null) : null,
    o.buyer ? fetch(`/api/export/${id}`).then((r) => (r.ok ? r.json() : null)).catch(() => null) : null]);
  details.set(o.order_id, { last_id: o.last_id, events, buyer, exp });
}

// Opened orders whose details are missing or older than the summary.
const stale = () => (summary?.orders || []).filter((o) => openOrders.has(o.order_id) && details.get(o.order_id)?.last_id !== o.last_id);

// After the seller acts (force) or on the timer: fetch, then repaint only if something changed.
async function render(force = true) {
  const changed = await loadSummary(force);
  if (!summary) return;
  const reload = stale();
  if (!changed && !reload.length) return;
  await Promise.all(reload.map(loadDetails));
  paint();
}

function paint() {
  const root = document.getElementById("order-list");
  invoiceRows = summary.invoices;
  renderInvoiceList(invoiceRows);
  renderOther(summary.side);
  showStats(summary.stats);
  showSyncStatus(summary.sync);
  const all = attentionItems(summary.orders, invoiceRows);
  const items = all.filter((it) => !it.waiting), waits = all.filter((it) => it.waiting);
  document.getElementById("attention").innerHTML = attentionHtml(items);
  document.getElementById("waiting").innerHTML = waitingHtml(waits);
  document.getElementById("waiting-group")?.addEventListener("toggle", (ev) => { waitingOpen = ev.currentTarget.open; });
  document.querySelectorAll("[data-attend]").forEach((b) => b.addEventListener("click", () => attend(items[Number(b.dataset.attend)])));
  document.querySelectorAll("[data-wait]").forEach((b) => b.addEventListener("click", () => attend(waits[Number(b.dataset.wait)])));
  root.innerHTML = summary.orders.length ? summary.orders.map((o) => orderRow(o)).join("")
    : '<div class="empty"><strong>No orders yet</strong>Pay one on the checkout page to see it here.</div>';
  if (deepLink) {
    const row = [...root.querySelectorAll(".order")].find((r) => r.dataset.orderId.endsWith(deepLink));
    deepLink = null;
    if (row) { setOpen(row, true); row.scrollIntoView({ block: "start" }); row.querySelector(".order-head").focus({ preventScroll: true }); }
  }
  root.querySelectorAll(".order-head").forEach((h) => h.addEventListener("click", () => {
    const row = h.closest(".order");
    setOpen(row, h.getAttribute("aria-expanded") !== "true");
  }));
  root.querySelectorAll("form.ship").forEach((f) => f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const button = f.querySelector("button");
    if (button.disabled) return; // already sending: a second click does nothing
    const msg = f.nextElementSibling;
    const data = Object.fromEntries(new FormData(f));
    button.disabled = true; button.textContent = "Saving…"; busy++;
    try {
      const res = await fetch(`/api/orders/${f.dataset.order}/tracking`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
      });
      if (res.ok) { UI.toast("Marked as shipped"); await render(); } else { msg.textContent = `Failed: ${await res.text()}`; }
    } finally { busy--; button.disabled = false; button.textContent = "Mark shipped"; }
  }));
  root.querySelectorAll(".buyer textarea").forEach((el) =>
    el.addEventListener("input", () => { if (el.value) el.dataset.dirty = "1"; else delete el.dataset.dirty; }));
  root.querySelectorAll("[data-reply]").forEach((b) => b.addEventListener("click", () => sendReply(b)));
  root.querySelectorAll("[data-refund]").forEach((b) => b.addEventListener("click", () => refund(b)));
  root.querySelectorAll("[data-purpose-save]").forEach((b) => b.addEventListener("click", () => savePurpose(b)));
  root.querySelectorAll("[data-purpose-suggest]").forEach((b) => b.addEventListener("click", () => suggestPurpose(b)));
  root.querySelectorAll("[data-fira]").forEach((c) => c.addEventListener("change", () => saveFira(c)));
  root.querySelectorAll("details.export").forEach((el) => el.addEventListener("toggle", () =>
    (el.open ? openExports.add(el.dataset.export) : openExports.delete(el.dataset.export))));
  root.querySelectorAll(".ai textarea, .ai input, .ai select").forEach((el) =>
    el.addEventListener("input", () => { el.dataset.dirty = "1"; }));
  root.querySelectorAll('[data-field="evidence_file"]').forEach((input) =>
    input.addEventListener("change", () => fileChosen(input)));
  root.querySelectorAll("[data-regen]").forEach((b) => b.addEventListener("click", () => regenerate(b)));
  root.querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", () => approve(b)));
  root.querySelectorAll("[data-sandbox]").forEach((b) => b.addEventListener("click", () => sandboxRequireEvidence(b)));
}

function showSyncStatus(s) {
  const auto = s.auto_sync_minutes > 0 ? `checks PayPal every ${s.auto_sync_minutes} min` : "automatic checks off";
  document.getElementById("sync-status").textContent = s.error ? `Last sync failed: ${s.error}`
    : s.last_synced_at ? `Synced ${fmt(s.last_synced_at)}, ${auto}` : `Not synced yet, ${auto}`;
}

// Sandbox-only test tool; the server refuses it (404) in live mode as well.
async function sandboxRequireEvidence(button) {
  if (!await UI.confirm({ title: "Sandbox test tool", confirm: "Ask PayPal Sandbox",
    what: ["Asks PayPal's Sandbox to move this dispute from \"PayPal is reviewing\" to waiting for your evidence.",
           "This only exists in the Sandbox. No money moves."] })) return;
  return withBusy(button, "Asking PayPal Sandbox…", async () => {
    const res = await fetch(`/api/disputes/${encodeURIComponent(button.dataset.sandbox)}/sandbox/require-seller-evidence`, { method: "POST" });
    if (!res.ok) throw new Error(await errorText(res));
    await render();
  });
}

function sendReply(button) {
  const box = button.closest(".req").querySelector("[data-reply-text]");
  const message = box.value.trim();
  if (!message) { box.focus(); return; }
  return withBusy(button, "Sending…", async () => {
    const res = await fetch(`/api/orders/${encodeURIComponent(button.dataset.order)}/requests/${encodeURIComponent(button.dataset.reply)}/reply`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message }),
    });
    if (!res.ok) throw new Error(`Reply failed: ${await errorText(res)}`);
    delete box.dataset.dirty; box.value = "";
    UI.toast("Reply sent. The buyer sees it on their order page.");
    await render();
  });
}

// Refunds move money: always ask first in the in-app dialog, and let the server check the
// amount again.
async function refund(button) {
  const { order, request, max, currency } = button.dataset;
  const partial = button.dataset.refund === "partial";
  let amount = max;
  if (partial) {
    amount = await UI.confirm({ title: "Refund part of the payment", tone: "money",
      input: { label: `Amount to refund (${currency})`, max, currency, verb: "Refund" },
      what: ["PayPal sends this amount back to the buyer from your balance.", "This can't be undone."] });
    if (!amount) return;
  } else if (!await UI.confirm({ title: "Refund the buyer", amount: `${max} ${currency}`, tone: "money", confirm: `Refund ${max} ${currency}`,
    what: ["PayPal sends the full amount back to the buyer from your balance.", "The buyer sees the refund on their order page.", "This can't be undone."] })) return;
  return withBusy(button, "Refunding…", async () => {
    const body = partial ? { mode: "partial", amount, request_id: request } : { mode: "full", request_id: request };
    const res = await fetch(`/api/orders/${encodeURIComponent(order)}/refund`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) { await render(); throw new Error(`Refund not sent: ${await errorText(res)}`); }
    UI.toast(`Refund of ${amount} ${currency} sent`);
    await render();
  });
}

async function withBusy(button, text, fn) {
  const card = button.closest(".ai, .sandbox-tool, .req, .export, .invoice-panel, [data-reminder], .policy") || document.querySelector(".invoice-panel");
  const msg = card.querySelector(".msg");
  const old = button.textContent;
  button.disabled = true; button.textContent = text; busy++;
  try { msg.textContent = (await fn()) || ""; msg.className = "msg"; }
  catch (err) { msg.textContent = err.message; msg.className = "msg bad"; }
  finally { busy--; button.disabled = false; button.textContent = old; }
}

async function errorText(res) {
  const body = await res.json().catch(() => ({}));
  const d = body.detail;
  if (d?.summary) return d.summary;
  if (d?.paypal_status) return `PayPal returned HTTP ${d.paypal_status}${d.paypal_error?.name ? ` (${d.paypal_error.name})` : ""}`;
  return typeof d === "string" ? d : JSON.stringify(d || body);
}

function regenerate(button) {
  return withBusy(button, "Thinking…", async () => {
    const res = await fetch(`/api/disputes/${encodeURIComponent(button.dataset.regen)}/recommendation`, { method: "POST" });
    if (!res.ok) throw new Error(`The AI couldn't answer: ${await errorText(res)}`);
    await render();
  });
}

const MAX_FILE_BYTES = 5 * 1024 * 1024; // the server checks again (type by content, too)

// Enable Approve only once a file is chosen; the server does the real checks.
function fileChosen(input) {
  const card = input.closest(".ai"), button = card.querySelector("[data-approve]"), msg = card.querySelector(".msg");
  const file = input.files[0];
  input.dataset.dirty = "1";
  const tooBig = file && file.size > MAX_FILE_BYTES;
  if (button) button.disabled = !file || !file.size || tooBig;
  msg.textContent = tooBig ? "That file is too big: PayPal accepts files of at most 5 MB." : "";
  msg.className = tooBig ? "msg bad" : "msg";
}

const toBase64 = (file) => new Promise((resolve, reject) => {
  const reader = new FileReader();
  reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
  reader.onerror = () => reject(new Error("Could not read the file. Please choose it again."));
  reader.readAsDataURL(file);
});

async function approve(button) {
  const card = button.closest(".ai");
  const field = (name) => card.querySelector(`[data-field="${name}"]`)?.value?.trim();
  const decision = button.dataset.decision;
  const body = { action: decision, message_to_buyer: field("message_to_buyer"), note_to_paypal: field("note_to_paypal") };
  if (decision === "OFFER") body.offer = { type: field("offer_type"), amount: Number(field("offer_amount")) };
  const file = button.dataset.needsFile ? card.querySelector('[data-field="evidence_file"]')?.files[0] : null;
  if (button.dataset.needsFile && !file) return; // stays disabled until a document is attached
  const typeLabel = card.querySelector('[data-field="evidence_type"]')?.selectedOptions[0]?.textContent;
  const amount = card.closest(".order")?.querySelector(".dispute .num")?.textContent || "";
  const cur = amount.split(" ")[1] || "";
  const plan = file
    ? { title: "Send your document to PayPal", what: [`Uploads "${file.name}" as "${typeLabel}" evidence.`, "Adds your note to PayPal."] }
    : { FIGHT: { title: "Send your evidence to PayPal", what: ["Sends your tracking to PayPal as proof of shipment, if PayPal allows it.", "Then sends your message to the buyer, if that's still allowed."] },
        OFFER: { title: "Make the buyer an offer", amount: `${body.offer?.amount} ${cur}`.trim(), what: [`Offers the buyer ${body.offer?.amount} ${cur} (${UI.word("offer", body.offer?.type)}).`, "Money moves only if the buyer accepts."] },
        REFUND: { title: "Accept the claim and refund", amount, what: ["Accepts the claim: PayPal refunds the full disputed amount to the buyer.", "The dispute closes. This can't be undone."] } }[decision];
  if (!await UI.confirm({ ...plan, confirm: button.textContent, tone: decision === "FIGHT" ? "" : "money" })) return;
  return withBusy(button, "Sending…", async () => {
    if (file) body.evidence = { type: field("evidence_type"), file_name: file.name, content_base64: await toBase64(file) };
    const res = await fetch(`/api/disputes/${encodeURIComponent(button.dataset.approve)}/act`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) { await render(); throw new Error(`PayPal: ${await errorText(res)}`); }
    UI.toast("Sent to PayPal");
    await render();
  });
}

// Webhook events that didn't match one of our orders (e.g. Simulator mock data).
// Closed by default; stays as the seller left it across refreshes.
let otherOpen = false;
function renderOther(side) {
  const root = document.getElementById("other");
  if (!side.length) { root.innerHTML = ""; return; }
  const count = side.reduce((n, o) => n + o.events.length, 0);
  const blocks = side.map((o) => `<div class="panel"><div class="mono small muted">${esc(o.order_id)}</div>${timelineRows(o.events, o.order_id)}</div>`);
  root.innerHTML = `<details class="panel disclose" id="other-events"${otherOpen ? " open" : ""}><summary><span class="d-title">Unmatched and other PayPal events</span>
      <span class="d-line">${count} in the last 7 days</span>${CHEV}</summary><div class="d-body">${blocks.join("")}</div></details>`;
  $("other-events").addEventListener("toggle", (ev) => { otherOpen = ev.currentTarget.open; });
}

// ---- Invoices: the AI drafts, the seller reviews, creates, sends and reminds ------------

const CURRENCIES = ["USD", "EUR", "GBP", "INR", "AUD", "CAD", "SGD", "AED", "JPY", "CHF"];
const $ = (id) => document.getElementById(id);
$("inv-currency").innerHTML = CURRENCIES.map((c) => `<option>${c}</option>`).join("");

const itemRow = (it = {}) => `<div class="item-row">
    <input data-item="name" placeholder="What you're billing for" maxlength="200" value="${esc(it.name || "")}" aria-label="Item" />
    <input data-item="quantity" type="number" min="0.00001" step="any" value="${esc(it.quantity ?? 1)}" aria-label="Quantity" />
    <input data-item="unit_amount" type="number" min="0.01" step="0.01" placeholder="Amount" value="${esc(it.unit_amount ?? "")}" aria-label="Amount" />
    <button class="secondary" data-item-remove aria-label="Remove line">✕</button></div>`;
function setItems(items) {
  $("inv-items").innerHTML = (items.length ? items : [{}]).map(itemRow).join("");
  $("inv-items").querySelectorAll("[data-item-remove]").forEach((b) => b.addEventListener("click", () => {
    b.closest(".item-row").remove();
    if (!$("inv-items").children.length) setItems([]);
  }));
}
setItems([]);
$("inv-add-item").addEventListener("click", () => setItems([...readItems(), {}]));
const readItems = () => [...$("inv-items").querySelectorAll(".item-row")].map((row) => ({
  name: row.querySelector('[data-item="name"]').value.trim(),
  quantity: Number(row.querySelector('[data-item="quantity"]').value),
  unit_amount: Number(row.querySelector('[data-item="unit_amount"]').value),
}));

$("invoice-extract").addEventListener("click", (ev) => withBusy(ev.currentTarget, "Drafting…", async () => {
  const res = await fetch("/api/invoices/extract", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: $("invoice-text").value }),
  });
  if (!res.ok) throw new Error(await errorText(res));
  const d = await res.json();
  $("inv-client").value = d.client_name || "";
  if (d.currency) $("inv-currency").value = d.currency;
  $("inv-due").value = d.due_date || "";
  $("inv-note").value = d.note || "";
  setItems(d.items || []);
  return `Draft filled in by AI (${UI.sentence(d.provider)}). Check it, type the client's email, then Create draft.`;
}));

$("invoice-create").addEventListener("click", (ev) => {
  const body = { client_name: $("inv-client").value.trim(), email: $("inv-email").value.trim(),
                 currency: $("inv-currency").value, items: readItems().filter((i) => i.name),
                 due_date: $("inv-due").value || null, note: $("inv-note").value.trim() || null };
  if (!body.client_name || !body.email || !body.items.length) { UI.notice("Missing details", "Fill in the client, their email and at least one line."); return; }
  return withBusy(ev.currentTarget, "Creating…", async () => {
    const res = await fetch("/api/invoices", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error(`Not created: ${await errorText(res)}`);
    const row = await res.json();
    await renderInvoices();
    return `Draft ${row.number || ""} created in PayPal. Send it from the list when you're ready.`;
  });
});

async function sendInvoice(button) {
  const r = invoiceRows.find((x) => x.invoice_id === button.dataset.invoiceSend) || {};
  if (!await UI.confirm({ title: "Send this invoice", amount: UI.money(r.amount, r.currency), confirm: "Send invoice",
    what: [`PayPal emails invoice ${r.number || ""} to ${r.client || "the client"} with a link to pay.`.replace("  ", " "), "You can't edit it after it's sent."] })) return;
  return withBusy(button, "Sending…", async () => {
    const res = await fetch(`/api/invoices/${encodeURIComponent(button.dataset.invoiceSend)}/send`, { method: "POST" });
    if (!res.ok) throw new Error(`Not sent: ${await errorText(res)}`);
    UI.toast("Invoice sent");
    await renderInvoices();
  });
}

// The AI (or a template) drafts the reminder; the seller edits it and clicks to send.
function draftReminder(button) {
  const id = button.dataset.invoiceRemind, cell = document.querySelector(`[data-reminder="${CSS.escape(id)}"]`);
  cell.parentElement.hidden = false;
  cell.innerHTML = '<span class="muted">Drafting…</span><div class="msg"></div>';
  return withBusy(button, "Drafting…", async () => {
    const res = await fetch(`/api/invoices/${encodeURIComponent(id)}/reminder-draft`, { method: "POST" });
    if (!res.ok) throw new Error(await errorText(res));
    const d = await res.json();
    cell.innerHTML = `<label class="field">Subject <input data-rem="subject" maxlength="100" value="${esc(d.subject)}" /></label>
      <label class="field">Note <textarea data-rem="note" maxlength="600">${esc(d.note)}</textarea></label>
      <div class="hint">${d.source === "ai" ? `Drafted by AI for ${esc(d.days_overdue)} days overdue.` : "Plain template (AI unavailable)."} Edit freely; nothing is sent until you click.</div>
      <div class="actions"><button data-rem-send="${esc(id)}">Send reminder</button></div><div class="msg"></div>`;
    cell.querySelectorAll("input, textarea").forEach((el) => el.addEventListener("input", () => { el.dataset.dirty = "1"; }));
    cell.querySelector("[data-rem-send]").addEventListener("click", (e) => sendReminder(e.currentTarget, cell));
  });
}

async function sendReminder(button, cell) {
  const subject = cell.querySelector('[data-rem="subject"]').value.trim(), note = cell.querySelector('[data-rem="note"]').value.trim();
  const r = invoiceRows.find((x) => x.invoice_id === button.dataset.remSend) || {};
  if (!await UI.confirm({ title: "Send this reminder", amount: UI.money(r.amount, r.currency), confirm: "Send reminder",
    what: [`PayPal emails your reminder to ${r.client || "the client"}.`, `Subject: ${subject}`] })) return;
  return withBusy(button, "Sending…", async () => {
    const res = await fetch(`/api/invoices/${encodeURIComponent(button.dataset.remSend)}/remind`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ subject, note }),
    });
    if (!res.ok) throw new Error(`Not sent: ${await errorText(res)}`);
    UI.toast("Reminder sent");
    await renderInvoices();
  });
}

// ---- Export paperwork (India) ---------------------------------------------------------

function savePurpose(button) {
  const code = button.closest(".export").querySelector("[data-purpose]").value;
  if (!code) { UI.notice("Choose a purpose code", "Pick the code that fits this payment first."); return; }
  return withBusy(button, "Saving…", async () => {
    const res = await fetch(`/api/export/${encodeURIComponent(button.dataset.purposeSave)}/purpose`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }),
    });
    if (!res.ok) throw new Error(await errorText(res));
    await render();
  });
}

function suggestPurpose(button) {
  return withBusy(button, "Thinking…", async () => {
    const res = await fetch(`/api/export/${encodeURIComponent(button.dataset.purposeSuggest)}/suggest`, { method: "POST" });
    if (!res.ok) throw new Error(await errorText(res));
    await render();
  });
}

async function saveFira(checkbox) {
  await fetch(`/api/export/${encodeURIComponent(checkbox.dataset.fira)}/fira`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ downloaded: checkbox.checked }),
  });
}

// ---- Business: the shop's name, country and how often to check PayPal ---------------------

async function loadBusiness() {
  const res = await fetch("/api/business");
  if (!res.ok) return;
  const b = await res.json(), form = $("business-form");
  for (const name of ["seller_name", "seller_country", "auto_sync_minutes"]) form.elements[name].value = b[name];
  $("business-line").textContent = `${b.seller_name}, ${b.seller_country}`;
}
$("business-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget, msg = form.querySelector(".msg");
  const body = { seller_name: form.elements.seller_name.value, seller_country: form.elements.seller_country.value,
                 auto_sync_minutes: Number(form.elements.auto_sync_minutes.value) };
  fetch("/api/business", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(async (res) => {
      msg.className = res.ok ? "msg" : "msg bad";
      msg.textContent = res.ok ? "Saved." : "Not saved. Check the name, the two-letter country code and the minutes.";
      if (res.ok) await loadBusiness();
    });
});
loadBusiness();

// ---- System status: the startup checks, run again on demand -------------------------------

const when = (t) => t ? new Date(t * 1000).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "Not since the app started";

function requestSeen(r) {
  const ip = (a) => `${esc(a.ip)} (${esc(a.kind)})`;
  const lines = [`Client IP we compute: ${ip(r.client)}`, `Connected from (as uvicorn reports it, untrusted): ${ip(r.peer)}`,
    `X-Forwarded-For: ${r.hops.length} hop${r.hops.length === 1 ? "" : "s"}, ${r.header_lines} header line${r.header_lines === 1 ? "" : "s"}`,
    ...r.hops.map((h, i) => `Hop ${i + 1} from the left: ${ip(h)}`),
    `Other address headers: ${r.headers.length ? r.headers.map(esc).join(", ") : "none"}`];
  return lines.map((line) => `<div>${line}</div>`).join("");
}

// The dispute assistant moves to Groq for good when Gemini fails, until someone switches back.
function aiProvider(state, demo) {
  if (!state) return "None";
  if (state.active_provider !== "groq") return esc({ gemini: "Gemini" }[state.active_provider] || state.active_provider);
  const since = state.switched_at ? ` since ${esc(when(state.switched_at))}` : "";
  const why = state.reason && state.reason !== "manual reset" ? ` (Gemini failed: ${esc(state.reason)})` : "";
  const button = demo ? "" : ' <button class="secondary small" data-ai-reset>Switch back to Gemini</button>';
  return `Using Groq${since}${why}${button}`;
}

function systemRows(s) {
  const mark = (ok) => ok === false ? '<span class="pill disputed">Problem</span> ' : "";
  const rows = s.checks.map((c) => [c.name, `${mark(c.ok)}${esc(c.detail)}`]);
  const sync = s.last_sync;
  rows.push(["Last webhook", s.last_webhook ? `${esc(when(s.last_webhook.at))}, ${esc(s.last_webhook.event_type)}` : "None yet"],
            ["Last sync", sync.error ? `${mark(false)}${esc(sync.error)}` : `${esc(when(sync.last_synced_at))}${sync.trigger ? ` (${esc(sync.trigger)})` : ""}`],
            ["Active AI", aiProvider(s.ai_provider, s.demo)],
            ["Database latency", s.db_latency_ms === null ? "Unknown" : `${esc(s.db_latency_ms)} ms`],
            ["App version", esc(s.version)],
            ["Request as seen by the server", requestSeen(s.request)]);
  return rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("");
}
async function loadSystem() {
  const res = await fetch("/api/system/status");
  if (!res.ok) throw new Error(await errorText(res));
  $("system-checks").innerHTML = systemRows(await res.json());
}
$("system").addEventListener("toggle", (ev) => { if (ev.currentTarget.open) loadSystem().catch(() => {}); });
$("system-run").addEventListener("click", (ev) => withBusy(ev.currentTarget, "Checking…", loadSystem));
$("system-checks").addEventListener("click", (ev) => {
  const button = ev.target.closest("[data-ai-reset]");
  if (!button) return;
  withBusy(button, "Switching…", async () => {
    const res = await fetch("/api/ai/reset", { method: "POST" });
    if (!res.ok) throw new Error(await errorText(res));
    await loadSystem();
    return "Back on Gemini.";
  });
});

// ---- Auto-resolve policy: the seller's own rules for refunds that need no click -----------

function policyForm(p) {
  const reasons = Object.entries(p.reasons).map(([k, v]) =>
    `<label class="check"><input type="checkbox" data-pol-reason="${esc(k)}"${p.allowed_reasons.includes(k) ? " checked" : ""} /> ${esc(v)}</label>`).join("");
  return `<label class="check switch"><input type="checkbox" data-pol="enabled"${p.enabled ? " checked" : ""} /> <strong>Refund matching requests automatically</strong></label>
    <fieldset><legend>Limits</legend><div class="grid">
      <label class="field">Currency <select data-pol="currency">${CURRENCIES.map((c) => `<option${c === p.currency ? " selected" : ""}>${c}</option>`).join("")}</select></label>
      <label class="field">Most per order <input type="number" min="0" step="0.01" inputmode="decimal" data-pol="max_refund_per_order" value="${esc(p.max_refund_per_order)}" /></label>
      <label class="field">Most per day, in total <input type="number" min="0" step="0.01" inputmode="decimal" data-pol="daily_cap" value="${esc(p.daily_cap)}" />
        <span class="hint">Used today: <span class="num">${esc(p.spent_today)}</span></span></label>
    </div></fieldset>
    <fieldset><legend>Only when</legend>
      <label class="check"><input type="checkbox" data-pol="require_no_dispute"${p.require_no_dispute ? " checked" : ""} /> The order has no dispute</label>
      <label class="check"><input type="checkbox" data-pol="only_if_not_shipped"${p.only_if_not_shipped ? " checked" : ""} /> The order hasn't shipped yet</label>
    </fieldset>
    <fieldset><legend>For these reasons</legend><div class="grid">${reasons}</div></fieldset>`;
}
async function loadPolicy() {
  const res = await fetch("/api/policy");
  if (!res.ok) return;
  const p = await res.json();
  $("policy-form").innerHTML = policyForm(p);
  $("policy-state").textContent = p.enabled ? `On, up to ${p.max_refund_per_order} ${p.currency} per order` : "Off";
  $("policy-state").className = p.enabled ? "pill settled" : "pill";
}
$("policy-save").addEventListener("click", async (ev) => {
  const button = ev.currentTarget;
  const form = $("policy-form"), val = (k) => form.querySelector(`[data-pol="${k}"]`);
  const body = { enabled: val("enabled").checked, currency: val("currency").value,
                 max_refund_per_order: val("max_refund_per_order").value || "0", daily_cap: val("daily_cap").value || "0",
                 require_no_dispute: val("require_no_dispute").checked, only_if_not_shipped: val("only_if_not_shipped").checked,
                 allowed_reasons: [...form.querySelectorAll("[data-pol-reason]:checked")].map((c) => c.dataset.polReason) };
  if (body.enabled && !await UI.confirm({ title: "Turn on automatic refunds", tone: "money", confirm: "Turn on auto-resolve",
    amount: `${body.max_refund_per_order} ${body.currency} per order`,
    what: [`Clearline refunds matching buyer requests right away, without asking you.`,
           `At most ${body.max_refund_per_order} ${body.currency} per order and ${body.daily_cap} ${body.currency} a day in total.`,
           "Anything outside these rules still comes to you."] })) return;
  return withBusy(button, "Saving…", async () => {
    const res = await fetch("/api/policy", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error(`Not saved: ${await errorText(res)}`);
    await loadPolicy();
    return "Saved.";
  });
});
loadPolicy();

document.getElementById("logout").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" }).catch(() => {});
  location.href = "/login";
});

// ---- Account: who's signed in, change password, sign out everywhere -----------------------

async function loadAccount() {
  const res = await fetch("/api/account");
  if (!res.ok) return;
  const me = await res.json();
  $("account-email").textContent = me.email;
  $("account-email-full").textContent = me.email;
  if (me.demo) lockForDemo();
}

// The server refuses these for a demo account; this says so before anyone tries.
function lockForDemo() {
  demoAccount = true;
  document.querySelectorAll(".demo-note").forEach((note) => { note.hidden = false; });
  document.querySelectorAll("#business-form :is(input, button), #password-form :is(input, button), #key-form :is(input, button), #signout-all, [data-revoke]")
    .forEach((el) => { el.disabled = true; });
}

$("password-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget, msg = f.querySelector(".msg"), button = f.querySelector("button[type=submit]");
  const body = { current: f.current.value, password: f.password.value, confirm: f.confirm.value };
  msg.className = "msg bad";
  if (!body.current || !body.password) { msg.textContent = "Fill in every field."; return; }
  if (body.password.length < 12) { msg.textContent = "Use at least 12 characters."; return; }
  if (body.password !== body.confirm) { msg.textContent = "The new passwords don't match."; return; }
  button.disabled = true; msg.textContent = "";
  try {
    const res = await fetch("/api/account/password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) { msg.textContent = typeof out.detail === "string" ? out.detail : "Couldn't change the password."; return; }
    f.reset(); msg.className = "msg"; msg.textContent = "Password changed. Your other devices are signed out.";
    UI.toast("Password changed");
  } finally { button.disabled = false; }
});

$("signout-all").addEventListener("click", async () => {
  if (!await UI.confirm({ title: "Sign out everywhere?", confirm: "Sign out everywhere",
    what: ["Every device signed in to this account is signed out, including this one."] })) return;
  await fetch("/api/account/sign-out-everywhere", { method: "POST" }).catch(() => {});
  location.href = "/login";
});

// ---- Agent access: API keys for the merchant MCP tools ----------------------------------

const keyRow = (k) => `<tr><td data-label="Name">${esc(k.name)}</td><td data-label="Key" class="mono">${esc(k.prefix)}…</td>
    <td data-label="Created">${esc(UI.date(k.created_at))}</td>
    <td data-label="Last used">${k.last_used_at ? esc(UI.dateTime(k.last_used_at)) : "Never"}</td>
    <td data-label="Status">${k.revoked ? '<span class="pill">Revoked</span>' : '<span class="pill settled">Active</span>'}</td>
    <td data-label="">${k.revoked ? "" : `<button class="secondary small" data-revoke="${esc(k.id)}" data-name="${esc(k.name)}">Revoke</button>`}</td></tr>`;

async function loadKeys() {
  const res = await fetch("/api/agent-keys");
  if (!res.ok) return;
  const keys = await res.json(), list = $("key-list");
  list.innerHTML = keys.length
    ? `<table class="tbl"><thead><tr><th>Name</th><th>Key</th><th>Created</th><th>Last used</th><th>Status</th><th><span class="visually-hidden">Actions</span></th></tr></thead><tbody>${keys.map(keyRow).join("")}</tbody></table>`
    : '<div class="empty"><strong>No agent keys yet</strong>Create one to connect your own AI agent.</div>';
  list.querySelectorAll("[data-revoke]").forEach((b) => { b.disabled = demoAccount; });
  list.querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
    if (!await UI.confirm({ title: `Revoke "${b.dataset.name}"?`, confirm: "Revoke key", tone: "danger",
      what: ["Agents using this key lose access right away. This can't be undone."] })) return;
    const r = await fetch(`/api/agent-keys/${encodeURIComponent(b.dataset.revoke)}`, { method: "DELETE" });
    if (r.ok) { UI.toast("Key revoked"); loadKeys(); }
  }));
}

$("key-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget, name = f.name.value.trim(), msg = $("agent-access").querySelector(".d-body > .msg");
  if (!name) { f.name.focus(); return; }
  const res = await fetch("/api/agent-keys", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
  if (!res.ok) { msg.className = "msg bad"; msg.textContent = "Couldn't create the key."; return; }
  const created = await res.json();
  $("new-key-value").textContent = created.key; // shown once, never stored in the page again
  $("new-key").hidden = false;
  f.reset();
  loadKeys();
});
$("new-key-copy").addEventListener("click", () => UI.copy($("new-key-value").textContent));
fetch("/api/agents").then((res) => res.ok && res.json()).then((info) => {
  if (info) $("mcp-url").textContent = new URL(info.mcp_url, location.origin).href;
});

loadAccount();
loadKeys();

document.getElementById("sync").addEventListener("click", async (ev) => {
  const button = ev.currentTarget, msg = document.getElementById("sync-msg");
  button.disabled = true; msg.textContent = "Checking PayPal for disputes…"; busy++;
  try {
    const res = await fetch("/api/disputes/sync", { method: "POST" });
    const c = await res.json();
    msg.textContent = res.ok
      ? `${c.listed} on PayPal: ${c.new} new, ${c.changed} changed, ${c.unchanged} unchanged${c.failed ? `, ${c.failed} failed` : ""}.`
      : `Sync failed: ${JSON.stringify(c.detail || c)}`; // textContent: no HTML parsing
    await render();
  } catch (err) { msg.textContent = `Sync failed: ${err.message}`; }
  finally { busy--; button.disabled = false; }
});

// Nav: highlight the section in view.
const navLinks = [...document.querySelectorAll('.shell-nav a[href^="#"]')];
if (typeof IntersectionObserver === "function") {
  const io = new IntersectionObserver((entries) => entries.forEach((en) => {
    if (!en.isIntersecting) return;
    navLinks.forEach((a) => (a.getAttribute("href") === `#${en.target.id}` ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  }), { rootMargin: "-30% 0px -60% 0px" });
  document.querySelectorAll("main > .section").forEach((s) => io.observe(s));
}

// Refreshing (below) waits while the seller is typing, has
// edited an AI draft, has a half-typed tracking number, or is waiting on a button.
function typing() {
  const active = document.activeElement;
  if (active && active.closest("#order-list, .invoices") && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)) return true;
  if (document.querySelector("#order-list [data-dirty], .invoices [data-dirty]")) return true;
  if (document.querySelector(".reminder-row:not([hidden])")) return true; // a reminder is being drafted
  if (document.querySelector("dialog[open]")) return true; // a confirm dialog is open
  return busy > 0 || [...document.querySelectorAll("#order-list form input")].some((i) => i.value);
}

fetch("/api/config").then((r) => r.json()).then((c) => { ENV = c.env; }).catch(() => {}).finally(render);
$("show-archived")?.addEventListener("change", () => render(true).catch(() => {}));

// Every 30 s while the tab is visible, and at once when the seller comes back to it.
const refresh = () => { if (!document.hidden && !typing()) render(false).catch(() => {}); };
setInterval(refresh, 30000);
document.addEventListener("visibilitychange", refresh);
window.addEventListener("focus", refresh);
