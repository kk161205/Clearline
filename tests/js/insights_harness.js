// Runs the Insights page's real <script> (with ui.js) in node, without AG Studio, and returns
// what the page builds from an /api/dashboard payload on stdin:
// payload -> {sources, defaultState, phoneState, moneyState: html}
const fs = require("fs");
const path = require("path");

const page = process.argv[2], dir = path.dirname(page);
const html = fs.readFileSync(page, "utf8");
global.document = { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] }; // no readyState: start() doesn't run
global.setInterval = () => 0;
new Function(fs.readFileSync(path.join(dir, "ui.js"), "utf8"))();
const script = fs.readFileSync(path.join(path.dirname(process.argv[2]), "pages", path.basename(process.argv[2], ".html") + ".js"), "utf8");
const view = new Function(script + "; return { layoutKey, layoutChanged, isAdvisory, toSources, defaultState, stackForPhone, moneyStateHtml, turnEvents, turnResponse, turnBody, configureTarget, panelsForAssistant };")();

const data = JSON.parse(fs.readFileSync(0, "utf8"));
const state = view.defaultState(data.charts.currency);
const sources = view.toSources(data);
// What each money field's display formatter shows (functions don't survive JSON).
const formatted = Object.fromEntries(sources.flatMap((src) => src.fields.filter((fd) => fd.createValueFormatter)
  .map((fd) => [`${src.id}.${fd.id}`, [1140, 900, 0.5, null].map((v) => fd.createValueFormatter()(v))])));
process.stdout.write(JSON.stringify({
  sources, formatted, defaultState: state, phoneState: view.stackForPhone(state),
  layout: {
    panelsOnly: view.layoutChanged({ ...state, panels: { ai: { collapsed: false } } }, view.layoutKey(state)),
    moved: view.layoutChanged({ ...state, selectedPageId: "other" }, view.layoutKey(state)),
    notLoaded: view.layoutChanged({ ...state, selectedPageId: "other" }, null),
  },
  advisory: { one: view.isAdvisory("1 issue", false), many: view.isAdvisory(" 3 issues ", false),
              failed: view.isAdvisory("1 issue", true), rows: view.isAdvisory("3 rows", false) },
  moneyState: view.moneyStateHtml([{ state: "Settled", amount: 300 }, { state: "Disputed", amount: 100 }, { state: "Waiting", amount: 0 }]),
  assistant: data.assistant && {
    events: view.turnEvents(data.assistant.reply), response: view.turnResponse(data.assistant.reply),
    body: view.turnBody(data.assistant.request), targets: data.assistant.states.map(view.configureTarget),
    panels: (data.assistant.panels || []).map(view.panelsForAssistant),
  },
}));
