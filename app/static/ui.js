// Clearline shared UI: plain words for PayPal codes, the clear line, toasts, the in-app
// confirm dialog (every money action goes through it), copy-to-clipboard and the theme toggle.
// Loaded in <head> so the saved theme applies before the first paint.
(function () {
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode: not remembered */ } },
  };
  const hasDom = typeof document !== "undefined" && typeof document.addEventListener === "function";

  // ---- Theme ----------------------------------------------------------------------------
  const THEME_KEY = "clearline-theme";
  function applyTheme(t) {
    if (!hasDom || !document.documentElement) return;
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
    else delete document.documentElement.dataset.theme;
  }
  applyTheme(store.get(THEME_KEY));
  function currentTheme() {
    const set = hasDom && document.documentElement.dataset.theme;
    if (set) return set;
    return typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function syncThemeButtons() {
    if (!hasDom) return;
    const dark = currentTheme() === "dark";
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => {
      b.setAttribute("aria-pressed", String(dark));
      const label = b.querySelector("[data-theme-label]") || b;
      label.textContent = dark ? "Switch to light theme" : "Switch to dark theme";
    });
  }
  function toggleTheme() {
    const next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next); store.set(THEME_KEY, next); syncThemeButtons();
  }

  // ---- Text helpers ---------------------------------------------------------------------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const money = (value, currency) => (value == null || value === "" ? "" : `${value} ${currency || ""}`.trim());
  // Day first ("27 Oct"); the year only when it isn't this year.
  const yearOf = (d) => (d.getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" });
  const dayOf = (d) => d.toLocaleDateString("en-GB", { day: "numeric", month: "short", ...yearOf(d) });
  const date = (t) => dayOf(new Date(t * 1000));
  const dateTime = (t) => { const d = new Date(t * 1000);
    return `${dayOf(d)}, ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`; };
  // "2026-10-18" (a date-only value, no time zone) -> "18 Oct".
  const isoDate = (s) => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || "");
    return m ? dayOf(new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))) : s || ""; };
  // "27 Oct 2026" from the server -> "27 Oct" in the current year.
  const shortDay = (s) => String(s || "").replace(new RegExp(` ${new Date().getFullYear()}$`), "");

  // ---- PayPal codes in plain words. Nothing raw reaches the page ---------------------------
  const WORDS = {
    status: {
      WAITING_FOR_SELLER_RESPONSE: "Your turn to respond", WAITING_FOR_BUYER_RESPONSE: "Waiting for the buyer",
      UNDER_REVIEW: "PayPal is reviewing", RESOLVED: "Resolved", OPEN: "Open", OTHER: "Other",
    },
    stage: { INQUIRY: "Inquiry", CHARGEBACK: "Claim", PRE_ARBITRATION: "Pre-arbitration", ARBITRATION: "Arbitration" },
    reason: {
      MERCHANDISE_OR_SERVICE_NOT_RECEIVED: "Item not received", MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED: "Item not as described",
      UNAUTHORISED: "Unauthorised payment", CREDIT_NOT_PROCESSED: "Refund not processed", DUPLICATE_TRANSACTION: "Charged twice",
      INCORRECT_AMOUNT: "Wrong amount charged", PAYMENT_BY_OTHER_MEANS: "Paid by other means",
      CANCELED_RECURRING_BILLING: "Cancelled subscription still billed", PROBLEM_WITH_REMITTANCE: "Problem with remittance", OTHER: "Other",
    },
    payment: {
      COMPLETED: "Completed", PENDING: "Pending", REFUNDED: "Refunded", PARTIALLY_REFUNDED: "Partly refunded",
      DECLINED: "Declined", FAILED: "Failed", REVERSED: "Reversed", CANCELLED: "Cancelled",
    },
    pending: {
      PENDING_REVIEW: "PayPal is reviewing the payment", RECEIVING_PREFERENCE_MANDATES_MANUAL_ACTION: "accept it in your PayPal account",
      UNILATERAL: "your PayPal account isn't confirmed yet", VERIFICATION_REQUIRED: "verify your PayPal account",
      ECHECK: "an eCheck that hasn't cleared", INTERNATIONAL_WITHDRAWAL: "accept it in your PayPal account", OTHER: "PayPal is holding it",
    },
    carrier: { DHL: "DHL", FEDEX: "FedEx", UPS: "UPS", USPS: "USPS" },
    decision: { FIGHT: "Fight", REFUND: "Refund", OFFER: "Offer" },
    invoice: {
      DRAFT: "Draft", SENT: "Sent", UNPAID: "Sent", SCHEDULED: "Scheduled", SHARED: "Sent", PAYMENT_PENDING: "Payment pending",
      PARTIALLY_PAID: "Partly paid", PAID: "Paid", MARKED_AS_PAID: "Paid", PAID_EXTERNAL: "Paid",
      CANCELLED: "Cancelled", AUTO_CANCELLED: "Cancelled", REFUNDED: "Refunded", PARTIALLY_REFUNDED: "Partly refunded",
      MARKED_AS_REFUNDED: "Refunded", REFUNDED_EXTERNAL: "Refunded",
    },
    outcome: { RESOLVED_SELLER_FAVOUR: "you won", RESOLVED_BUYER_FAVOUR: "buyer won", RESOLVED_WITH_PAYOUT: "PayPal covered it",
               CANCELED_BY_BUYER: "buyer cancelled", ACCEPTED: "you accepted", DENIED: "denied", NONE: "no outcome" },
    offer: { REFUND: "Refund", REFUND_WITH_RETURN: "Refund after return", REFUND_WITH_REPLACEMENT: "Replacement", REPLACEMENT_WITHOUT_REFUND: "Replacement" },
  };
  // Last resort for a code we haven't mapped: "CHECKOUT.ORDER.APPROVED" -> "Checkout order approved".
  const sentence = (code) => {
    const s = String(code || "").toLowerCase().replace(/[._-]+/g, " ").trim();
    return s ? s[0].toUpperCase() + s.slice(1) : "";
  };
  const isCode = (s) => /^[A-Z0-9_.-]+$/.test(s); // already plain words (e.g. "FedEx") pass through
  const word = (kind, code) => (code == null || code === "" ? "" : WORDS[kind]?.[code] || (isCode(String(code)) ? sentence(code) : String(code)));

  // ---- The clear line ---------------------------------------------------------------------
  // stops: [{key, title, when, detail (HTML), tone, seg, minor, ahead}]. tone colours the dot;
  // seg is the money state from this stop on and colours the segment to the next stop, so the
  // line changes colour at the event that changed it. Remembers which stops each line has
  // shown, so only a stop that arrives later gets the draw-in (never on first load).
  const seen = new Map();
  function line(id, stops) {
    const before = seen.get(id);
    const now = new Set();
    const items = stops.map((s, i) => {
      const key = s.key ?? `${i}`;
      now.add(key);
      const fresh = before && !s.ahead && !before.has(key);
      const cls = ["cl-stop", s.minor && "minor", s.ahead && "ahead", fresh && "cl-new"].filter(Boolean).join(" ");
      return `<li class="${cls}"${s.tone ? ` data-tone="${esc(s.tone)}"` : ""} data-seg="${esc(s.seg || "none")}"><span class="cl-dot" aria-hidden="true"></span>
        <div class="cl-body"><div class="cl-title">${esc(s.title)}</div>${s.when ? `<div class="cl-when">${esc(s.when)}</div>` : ""}${s.detail || ""}</div></li>`;
    });
    seen.set(id, now);
    return `<ol class="cl" data-line="${esc(id)}">${items.join("")}</ol>`;
  }

  // ---- Toasts -------------------------------------------------------------------------------
  function toast(text, tone = "") {
    if (!hasDom) return;
    let box = document.querySelector(".toasts");
    if (!box) {
      box = document.createElement("div");
      box.className = "toasts"; box.setAttribute("role", "status"); box.setAttribute("aria-live", "polite");
      document.body.appendChild(box);
    }
    const el = document.createElement("div");
    el.className = `toast ${tone}`; el.textContent = text;
    box.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  // ---- In-app confirm --------------------------------------------------------------------------
  // UI.confirm({title, amount, what: [sentences], confirm: "Refund 40.00 USD", tone: "money"|"danger"|"",
  //             input: {label, max, currency, verb}}) -> Promise: false/true, or the typed amount.
  function confirmDialog(opts) {
    return new Promise((resolve) => {
      const d = document.createElement("dialog");
      d.className = "ui-dialog";
      const input = opts.input;
      d.innerHTML = `<form method="dialog">
          <h2 id="ui-dialog-title">${esc(opts.title)}</h2>
          ${opts.amount ? `<div class="amount" data-amount>${esc(opts.amount)}</div>` : ""}
          ${input ? `<label class="field">${esc(input.label)}<input data-input inputmode="decimal" autocomplete="off" placeholder="0.00" value="${esc(input.value || "")}" />
            <span class="hint">At most ${esc(money(input.max, input.currency))}</span></label><div class="error" data-error></div>` : ""}
          ${(opts.what || []).length ? `<ul>${opts.what.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
          <div class="actions">
            <button type="button" class="secondary" data-cancel>Cancel</button>
            <button type="submit" class="${esc(opts.tone || "")}" data-ok>${esc(opts.confirm || "Continue")}</button>
          </div></form>`;
      d.setAttribute("aria-labelledby", "ui-dialog-title");
      document.body.appendChild(d);
      const ok = d.querySelector("[data-ok]"), field = d.querySelector("[data-input]"), error = d.querySelector("[data-error]");
      const valid = () => {
        const v = (field.value || "").trim();
        return /^\d+(\.\d{1,2})?$/.test(v) && Number(v) > 0 && Number(v) <= Number(input.max) ? v : null;
      };
      if (field) {
        const sync = () => {
          const v = valid();
          ok.textContent = v ? `${input.verb} ${Number(v).toFixed(2)} ${input.currency}` : `${input.verb}`;
          ok.disabled = !v;
        };
        field.addEventListener("input", () => { error.textContent = ""; sync(); });
        sync();
      }
      let result = false;
      d.querySelector("[data-cancel]").addEventListener("click", () => d.close());
      d.querySelector("form").addEventListener("submit", (ev) => {
        if (field) {
          const v = valid();
          if (!v) { ev.preventDefault(); error.textContent = `Enter an amount between 0.01 and ${input.max}.`; return; }
          result = Number(v).toFixed(2);
        } else result = true;
      });
      d.addEventListener("close", () => { d.remove(); resolve(result); });
      d.showModal();
      (field || d.querySelector("[data-cancel]")).focus(); // Cancel first: Enter never moves money by accident
    });
  }

  // A plain message in the same dialog (instead of alert()).
  const notice = (title, text) => confirmDialog({ title, what: [text], confirm: "OK" });

  // ---- Copy ---------------------------------------------------------------------------------
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast("Copied"); return true; }
    catch { toast("Couldn't copy. Select the text and copy it yourself.", "bad"); return false; }
  }

  if (hasDom) {
    document.addEventListener("click", (ev) => {
      const t = ev.target.closest?.("[data-copy], [data-theme-toggle]");
      if (!t) return;
      if (t.hasAttribute("data-theme-toggle")) toggleTheme();
      else copy(t.dataset.copy);
    });
    document.addEventListener("DOMContentLoaded", syncThemeButtons);
  }

  globalThis.UI = { esc, money, date, dateTime, isoDate, shortDay, word, sentence, WORDS, line, toast, confirm: confirmDialog, notice, copy, toggleTheme };
})();
