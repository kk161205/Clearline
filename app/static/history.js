// The clear line for an order or invoice: plain-words stops, money-state segments and the
// latest dispute state. Shared by /seller and /dashboard (classic script: these are globals).
const esc = UI.esc;
const fmt = (t) => UI.dateTime(t);
const money = (a) => (a && a.value ? UI.money(a.value, a.currency_code) : "");
const reasonWords = (code) => UI.word("reason", code) || "Unknown reason";
const STEP_NAMES = { "provide-evidence": "Evidence", "send-message": "Message to the buyer", "make-offer": "Offer", "accept-claim": "Refund" };
const DISPUTE_KINDS = ["dispute_created", "dispute_updated", "dispute_resolved"];
const DECISIONS = UI.WORDS.decision;
const BUYER_TYPES = { unrecognised: "Doesn't recognise the charge", not_arrived: "Order hasn't arrived", refund: "Wants a refund", message: "Sent a message" };

// The server stores PayPal failures as a short sentence plus issue/debug_id. Rows from
// before that hold PayPal's raw error text, so pull the same facts out of it.
const LEGACY_ISSUES = {
  ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE: "PayPal didn't allow this step in the dispute's current state",
  EVIDENCE_TYPE_IS_NOT_ALLOWED: "PayPal won't accept this kind of evidence at this stage. Attach a document and choose the matching type.",
};
function errorParts(p, field = "error") {
  const raw = String(p[field] || "");
  const legacy = raw.includes("PayPal API error");
  const issue = p.paypal_issue || (raw.match(/'issue': '([A-Z_]+)'/) || [])[1];
  const debug = p.debug_id || (raw.match(/'debug_id': '([\w-]+)'/) || [])[1];
  const status = p.paypal_status || (raw.match(/PayPal API error (\d+)/) || [])[1];
  // Rows stored before an issue had its own sentence still show the plain-words version.
  const text = LEGACY_ISSUES[issue] || (legacy ? "PayPal rejected this step" : raw);
  // PayPal's full error, for support: a header line, then one line per details[] entry.
  // It stays collapsed under "details" (the only place a raw PayPal code is shown).
  const e = p.paypal_error;
  const tech = e
    ? [[e.http_status && `HTTP ${e.http_status}`, e.name, e.message, e.debug_id && `debug_id ${e.debug_id}`].filter(Boolean).join(" · "),
       ...(e.details || []).map((d) => [d.issue, d.field && `field ${d.field}`, d.value != null && `value ${d.value}`,
                                         d.location && `location ${d.location}`, d.description].filter(Boolean).join(" · "))]
    : [[status && `HTTP ${status}`, issue && `issue ${issue}`, debug && `debug_id ${debug}`].filter(Boolean).join(" · ")];
  return { text, tech: tech.filter(Boolean) };
}
const techHtml = (lines) => lines.length
  ? `<details class="tech"><summary>details</summary>${lines.map((l) => `<div>${esc(l)}</div>`).join("")}</details>` : "";
function errorHtml(p, field = "error") {
  const { text, tech } = errorParts(p, field);
  return esc(text) + techHtml(tech);
}

// ---- The clear line: one stop per event, in plain words ------------------------------------

const detail = (text) => (text ? `<div class="cl-detail">${esc(text)}</div>` : "");
const PAYPAL_FAILURE_KINDS = ["dispute_action_failed", "dispute_action_skipped", "sandbox_status_change_failed", "refund_failed", "invoice_action_failed"];
const INVOICE_TONE = { Paid: "settled", Sent: "held", "Payment pending": "held", "Partly paid": "held", Scheduled: "held" };
const disputeName = (p) => (p.dispute_life_cycle_stage ? UI.word("stage", p.dispute_life_cycle_stage) : "Dispute");

// {title, detail (HTML), tone, minor} for one timeline event.
function stop(e) {
  const p = e.payload, k = e.kind;
  switch (k) {
    case "order_created": return { title: "Order created", tone: "none", detail: detail(UI.money(p.amount, p.currency)) };
    case "payment_captured": return { title: "Payment received", tone: "settled",
      detail: detail([p.payer_email && `From ${p.payer_email}`, p.payer_country && `Country: ${p.payer_country}`].filter(Boolean).join(", ")) };
    case "capture_failed": return { title: "Payment didn't go through", tone: "disputed" };
    case "tracking_added": return { title: `Shipped with ${UI.word("carrier", p.carrier)}`, tone: "settled",
      detail: p.tracking_number ? `<div class="cl-detail">Tracking <span class="num">${esc(p.tracking_number)}</span></div>` : "" };
    case "dispute_created": return { title: `${disputeName(p)} opened: ${reasonWords(p.reason)}`, tone: "disputed",
      detail: detail([money(p.dispute_amount), UI.word("status", p.status)].filter(Boolean).join(", ")) };
    case "dispute_updated": return { title: `${disputeName(p)} update: ${UI.word("status", p.status) || "changed"}`, tone: "disputed",
      detail: detail(money(p.dispute_amount)) };
    case "dispute_resolved": return { title: p.outcome ? `Dispute closed: ${UI.word("outcome", p.outcome)}` : "Dispute closed", tone: "settled", detail: detail(reasonWords(p.reason)) };
    case "webhook_payment_capture_completed": return { title: "PayPal confirmed the payment", tone: "settled", minor: true, detail: detail(money(p.amount)) };
    case "webhook_payment_capture_pending": return { title: "PayPal is holding the payment", tone: "held",
      detail: detail(p.pending_reason ? `Waiting on: ${UI.word("pending", p.pending_reason)}` : "") };
    case "webhook_payment_capture_refunded": return { title: "PayPal confirmed a refund", tone: "settled", minor: true, detail: detail(money(p.amount)) };
    case "webhook_payment_capture_reversed": return { title: "Payment reversed", tone: "disputed", detail: detail(money(p.amount)) };
    case "webhook_other": return { title: UI.sentence(p.event_type) || "PayPal update", minor: true };
    case "webhook_processing_failed": return { title: "Couldn't process a PayPal update", tone: "disputed", minor: true, detail: detail(p.error) };
    case "ai_recommendation": {
      const r = p.recommendation || {};
      return { title: `AI suggested: ${DECISIONS[r.decision] || "no decision"}`, minor: true }; // confidence: on the card
    }
    case "ai_failed": return { title: "AI couldn't make a suggestion", minor: true, detail: detail(p.error) };
    case "ai_provider_switched": return { title: `AI switched to ${UI.sentence(p.to)}`, minor: true, detail: detail(p.reason) };
    case "dispute_action_sent": return { title: `Sent to PayPal: ${STEP_NAMES[p.endpoint] || UI.sentence(p.endpoint)}`, tone: "settled" };
    case "dispute_action_failed": return { title: `PayPal didn't accept: ${STEP_NAMES[p.endpoint] || UI.sentence(p.endpoint)}`, tone: "disputed", detail: detail(errorParts(p).text) };
    case "dispute_action_skipped": return { title: `Skipped: ${STEP_NAMES[p.endpoint] || UI.sentence(p.endpoint)}`, minor: true, detail: detail(p.reason) };
    case "sandbox_status_changed": return { title: "Sandbox: PayPal now asks for your evidence", minor: true };
    case "sandbox_status_change_failed": return { title: "Sandbox tool failed", tone: "disputed", minor: true, detail: detail(errorParts(p).text) };
    case "buyer_request": return { title: `Buyer: ${BUYER_TYPES[p.type] || "Sent a request"}`, tone: "held",
      detail: detail([p.amount && `Asks for ${p.amount}`, p.message].filter(Boolean).join(". ")) + (p.via === "agent" ? '<span class="pill plain">via AI agent</span>' : "") };
    case "seller_reply": return { title: p.auto ? "Automatic reply" : "You replied", tone: "none", detail: detail(p.message) };
    case "refund_sent": return { title: `Refunded ${UI.money(p.amount, p.currency)}`, tone: "settled",
      detail: detail([p.mode === "full" ? "Full refund" : "Partial refund", p.status && p.status !== "COMPLETED" && `PayPal: ${UI.word("payment", p.status)}`,
                      p.trigger === "auto-policy" && `Automatic, by your policy: ${p.rule}`].filter(Boolean).join(". ")) };
    case "refund_failed": return { title: "Refund didn't go through", tone: "disputed", detail: detail(`${UI.money(p.amount, p.currency)}: ${errorParts(p).text}`) };
    case "auto_policy_skipped": return { title: "Auto-resolve didn't apply", minor: true, detail: detail(`${p.reason}, so it's waiting for you`) };
    case "auto_policy_updated": return { title: p.enabled ? "Auto-resolve turned on" : "Auto-resolve turned off", minor: true,
      detail: detail(p.enabled ? `Up to ${p.max_refund_per_order} ${p.currency} per order, ${p.daily_cap} per day` : "") };
    case "invoice_status": {
      const s = UI.word("invoice", p.status);
      return { title: `Invoice status: ${s}`, tone: INVOICE_TONE[s] || "none",
               detail: detail([p.amount && UI.money(p.amount, p.currency), p.due_date && `Due ${UI.isoDate(p.due_date)}`].filter(Boolean).join(", ")) };
    }
    case "invoice_send_sent": return { title: "Invoice sent", tone: "held" };
    case "invoice_reminder_sent": return { title: "Reminder sent", tone: "held", detail: detail(p.subject) };
    case "invoice_paid": return { title: "Paid", tone: "settled", detail: detail(p.amount && UI.money(p.amount, p.currency)) };
    case "invoice_action_failed": return { title: "Invoice action failed", tone: "disputed", detail: detail(errorParts(p).text) };
    default: return { title: UI.sentence(k), minor: true };
  }
}

// The order's money state, which colours its whole line.
function orderTone(events, state) {
  const kinds = new Set(events.map((e) => e.kind));
  if (state && state.kind !== "dispute_resolved" && state.payload.status !== "RESOLVED") return "disputed";
  if (kinds.has("webhook_payment_capture_reversed")) return "disputed";
  const paid = kinds.has("payment_captured") || kinds.has("invoice_paid");
  const lastPending = events.filter((e) => e.kind === "webhook_payment_capture_pending").pop();
  const lastDone = events.filter((e) => e.kind === "webhook_payment_capture_completed").pop();
  if (lastPending && (!lastDone || lastDone.created_at < lastPending.created_at)) return "held";
  if (openRequests(events).length) return "held";
  if (paid) return "settled";
  if (kinds.has("invoice_status")) return "held";
  return "none";
}

// Buyer requests with no reply and no automatic refund yet.
function openRequests(events) {
  const answered = new Set(events.filter((e) => e.kind === "seller_reply").map((e) => e.payload.request_id));
  return events.filter((e) => e.kind === "buyer_request" && !answered.has(e.payload.request_id));
}

// The money state from each event on, so the line changes colour at the event that changed
// it: crimson from a claim opening until it closes, amber from a hold or an unanswered buyer
// question until it clears, teal once paid, grey before.
function segments(events) {
  let paid = false, dispute = false, reversed = false, pending = false, invoiceDue = false;
  const asked = new Set();
  return events.map((e) => {
    const p = e.payload;
    if (e.kind === "payment_captured" || e.kind === "invoice_paid") paid = true;
    if (e.kind === "webhook_payment_capture_pending") pending = true;
    if (e.kind === "webhook_payment_capture_completed") pending = false;
    if (e.kind === "webhook_payment_capture_reversed") reversed = true;
    if (e.kind === "dispute_created" || e.kind === "dispute_updated") dispute = p.status !== "RESOLVED";
    if (e.kind === "dispute_resolved") dispute = false;
    if (e.kind === "buyer_request") asked.add(p.request_id);
    if (e.kind === "seller_reply") asked.delete(p.request_id);
    if (e.kind === "invoice_status") invoiceDue = !["Paid", "Cancelled", "Draft", "Refunded"].includes(UI.word("invoice", p.status));
    return dispute || reversed ? "disputed" : pending || asked.size || (invoiceDue && !paid) ? "held" : paid ? "settled" : "none";
  });
}

// Stops, keeping only the latest AI recommendation per dispute in place; earlier ones fold
// into "(+N earlier)" on that stop. Ahead: what hasn't happened yet (grey, dashed).
function timelineRows(events, id = "timeline") {
  const seg = segments(events);
  const recs = {};
  events.filter((e) => e.kind === "ai_recommendation").forEach((e) => (recs[e.payload.dispute_id] ||= []).push(e));
  const stops = [];
  events.forEach((e, i) => {
    let extra = "";
    if (e.kind === "ai_recommendation") {
      const list = recs[e.payload.dispute_id];
      if (e !== list[list.length - 1]) return;
      const earlier = list.slice(0, -1);
      if (earlier.length) extra = `<details class="tech"><summary>${earlier.length} earlier</summary>${earlier.map((x) =>
        `<div>${esc(stop(x).title)}, ${esc(fmt(x.created_at))}</div>`).join("")}</details>`;
    }
    const s = stop(e);
    if (PAYPAL_FAILURE_KINDS.includes(e.kind)) extra += techHtml(errorParts(e.payload).tech);
    stops.push({ ...s, seg: seg[i], key: `${e.kind}-${e.created_at}-${i}`, when: fmt(e.created_at), detail: (s.detail || "") + extra });
  });
  const state = currentDispute(events);
  const kinds = new Set(events.map((e) => e.kind));
  if (state && !isResolved(state) && state.payload.seller_response_due_date) {
    const due = new Date(state.payload.seller_response_due_date);
    if (!isNaN(due)) stops.push({ key: "ahead-due", ahead: true, title: `Respond by ${UI.date(due / 1000)}` });
  } else if (kinds.has("payment_captured") && !kinds.has("tracking_added") && !kinds.has("invoice_paid") && !kinds.has("refund_sent")) {
    stops.push({ key: "ahead-ship", ahead: true, title: "Not shipped yet" });
  }
  return UI.line(id, stops);
}

// Current dispute state = the event with the newest PayPal update_time, not the last row
// inserted (webhooks can arrive out of order). Rows without update_time count as oldest.
function currentDispute(events) {
  const states = events.filter((e) => DISPUTE_KINDS.includes(e.kind));
  const t = (e) => Date.parse(e.payload.update_time || "") || 0;
  return states.reduce((best, e) => (!best || t(e) >= t(best) ? e : best), null);
}

const isResolved = (state) => state.kind === "dispute_resolved" || state.payload.status === "RESOLVED";
