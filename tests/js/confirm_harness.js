// Runs the seller page's real <script> in node and clicks every money action with the in-app
// confirm stubbed to answer `answer` (true/false, or an amount for partial refunds).
// stdin: {answer} -> stdout: [{action, confirms: [opts], fetches: ["POST url body"], native: [name]}]
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(process.argv[2], "utf8");
// history.js is a classic <script> before the page's own, so its globals come first.
const history = fs.readFileSync(path.join(path.dirname(process.argv[2]), "history.js"), "utf8");
const script = [history, html.match(/<script>([\s\S]*)<\/script>/)[1]].join(";\n");
const { answer } = JSON.parse(fs.readFileSync(0, "utf8"));

const el = (props = {}) => ({ addEventListener() {}, querySelector: () => null, querySelectorAll: () => [],
                              dataset: {}, value: "", textContent: "", ...props });
const handlers = {}; // click handlers the page wires by element id (the policy Save button)
const byId = (id) => el({ addEventListener: (type, fn) => { handlers[id] = fn; },
                          querySelector: () => (id === "policy-form" ? el({ checked: true, value: "10" }) : null) });
global.document = { getElementById: byId, querySelector: () => null, querySelectorAll: () => [] };
global.location = { origin: "https://shop.example" };
global.setInterval = () => 0;
new Function(fs.readFileSync(path.join(path.dirname(process.argv[2]), "ui.js"), "utf8"))();

let log = { confirms: [], fetches: [], native: [] }; // page start-up loads land here
const native = (name) => () => { log.native.push(name); return true; }; // browser dialogs must never be used
global.confirm = native("confirm"); global.prompt = native("prompt"); global.alert = native("alert");
UI.confirm = async (opts) => { log.confirms.push(opts); return answer; };
UI.toast = () => {};
// Reads never answer (the page doesn't render on its own); writes are recorded and succeed.
global.fetch = (url, opts = {}) => {
  if ((opts.method || "GET") === "GET") return new Promise(() => {});
  log.fetches.push(`${opts.method} ${url} ${opts.body || ""}`.trim());
  return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
};

const page = new Function(script + "; return { refund, approve, sendInvoice, sendReminder, sandboxRequireEvidence, render };")();

const msg = el();
const card = el({ querySelector: (sel) => (sel === ".msg" ? msg : sel.includes("data-field") && !sel.includes("evidence") ? el({ value: "40" }) : null),
                  closest: () => el({ querySelector: () => el({ textContent: "120.00 USD" }) }) });
const button = (dataset, text = "Button") => el({ dataset, textContent: text, closest: () => card });

const actions = {
  "refund full": () => page.refund(button({ refund: "full", order: "O1", request: "R1", max: "120.00", currency: "USD" })),
  "refund partial": () => page.refund(button({ refund: "partial", order: "O1", request: "R1", max: "120.00", currency: "USD" })),
  "dispute refund": () => page.approve(button({ approve: "PP-D-1", decision: "REFUND" }, "Refund 120.00 USD")),
  "dispute offer": () => page.approve(button({ approve: "PP-D-1", decision: "OFFER" }, "Offer 40.00 USD")),
  "dispute fight": () => page.approve(button({ approve: "PP-D-1", decision: "FIGHT" }, "Send evidence")),
  "send invoice": () => page.sendInvoice(button({ invoiceSend: "INV2-1" })),
  "send reminder": () => page.sendReminder(button({ remSend: "INV2-1" }), el({ querySelector: () => el({ value: "Hi" }) })),
  "policy on": () => handlers["policy-save"]({ currentTarget: button({}) }),
  "sandbox tool": () => page.sandboxRequireEvidence(button({ sandbox: "PP-D-1" })),
};

(async () => {
  const out = [];
  for (const [action, run] of Object.entries(actions)) {
    log = { action, confirms: [], fetches: [], native: [] };
    run(); // after a write the page re-renders, which waits on reads forever: don't await
    await new Promise((r) => setTimeout(r, 20));
    out.push(log);
  }
  process.stdout.write(JSON.stringify(out));
})();
