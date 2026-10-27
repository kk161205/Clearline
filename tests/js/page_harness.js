// Runs the seller page's real <script> in node with stubbed browser globals and renders the
// dispute area for each scenario read from stdin: [{env, events}] -> [{sandbox, compact, approve}].
const fs = require("fs");

const html = fs.readFileSync(process.argv[2], "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const stub = { addEventListener() {}, querySelector: () => null, querySelectorAll: () => [] };
global.document = { getElementById: () => stub, querySelector: () => null, querySelectorAll: () => [] };
global.fetch = () => new Promise(() => {}); // never resolves: the page doesn't render on its own
global.setInterval = () => 0;

const page = new Function(script +
  "; return { currentDispute, disputePanel, aiCard, setEnv: (e) => { ENV = e; } };")();

const scenarios = JSON.parse(fs.readFileSync(0, "utf8"));
const results = scenarios.map(({ env, events }) => {
  page.setEnv(env);
  const state = page.currentDispute(events);
  const d = page.disputePanel(state);
  const out = d.panel + page.aiCard(events, state, d.resolved); // what render() puts on the card
  return { sandbox: out.includes("data-sandbox"), compact: out.includes("ai compact"),
           approve: out.includes("data-approve") };
});
process.stdout.write(JSON.stringify(results));
