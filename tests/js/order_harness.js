// Runs the buyer page's real <script> (app/static/order.html) in node with stubbed browser
// globals and returns view(data, open) HTML for each scenario read from stdin:
// [{data, open}] -> [html]
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(process.argv[2], "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const stub = { addEventListener() {}, querySelector: () => null, querySelectorAll: () => [], innerHTML: "" };
global.document = { getElementById: () => stub, querySelector: () => null, querySelectorAll: () => [] };
global.location = { pathname: "/o/test-token" };
global.fetch = () => new Promise(() => {}); // never resolves: the page doesn't load on its own
global.setInterval = () => 0;
// The shared design system first (the page uses UI.*), as <script src="/static/ui.js"> does.
new Function(fs.readFileSync(path.join(path.dirname(process.argv[2]), "ui.js"), "utf8"))();

const page = new Function(script + "; return { view };")();
const scenarios = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(scenarios.map(({ data, open }) => page.view(data, open || null))));
