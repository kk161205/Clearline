// Runs the seller page's real <script> in node with stubbed browser globals and renders the
// dispute area for each scenario read from stdin:
// [{env, events, buyer?, exportData?}] -> [{sandbox, compact, approve, card, timeline, buyer, export}]
// (card / timeline / buyer / export are HTML).
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(process.argv[2], "utf8");
// history.js is a classic <script> before the page's own, so its globals come first.
const history = fs.readFileSync(path.join(path.dirname(process.argv[2]), "history.js"), "utf8");
const script = [history, fs.readFileSync(path.join(path.dirname(process.argv[2]), "pages", path.basename(process.argv[2], ".html") + ".js"), "utf8")].join(";\n");
const stub = { addEventListener() {}, querySelector: () => null, querySelectorAll: () => [] };
global.document = { getElementById: () => stub, querySelector: () => null, querySelectorAll: () => [], addEventListener() {} };
global.window = { addEventListener() {} };
global.fetch = () => new Promise(() => {}); // never resolves: the page doesn't render on its own
global.setInterval = () => 0;
// The shared design system first (the page uses UI.*), as <script src="/static/ui.js"> does.
new Function(fs.readFileSync(path.join(path.dirname(process.argv[2]), "ui.js"), "utf8"))();

global.location = { origin: "https://shop.example" };

const page = new Function(script +
  "; return { currentDispute, disputePanel, aiCard, timelineRows, buyerSection, exportSection, attentionItems, canShip, aiProvider," +
  " setEnv: (e) => { ENV = e; } };")();

const scenarios = JSON.parse(fs.readFileSync(0, "utf8"));
const results = scenarios.map(({ env, events, buyer, exportData, ai, demo, invoices }) => {
  page.setEnv(env);
  const state = page.currentDispute(events || []);
  const d = page.disputePanel(state);
  const out = d.panel + page.aiCard(events || [], state, d.resolved); // what render() puts on the card
  return { sandbox: out.includes("data-sandbox"), compact: out.includes("ai compact"),
           approve: out.includes("data-approve"), card: out, timeline: page.timelineRows(events || []),
           buyer: buyer ? page.buyerSection("ORDER1", buyer) : "",
           export: exportData ? page.exportSection("ORDER1", exportData) : "",
           canShip: page.canShip(events || [], buyer || null),
           ai: ai === undefined ? null : page.aiProvider(ai, Boolean(demo)),
           today: page.attentionItems([{ order_id: "ORDER000000001", events: events || [], amount: "120.00 USD", buyer: buyer || null }], invoices || []) };
});
process.stdout.write(JSON.stringify(results));
