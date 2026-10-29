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
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const view = new Function(script + "; return { toSources, defaultState, stackForPhone, moneyStateHtml, turnEvents, turnResponse, turnBody, configureTarget, panelsForAssistant };")();

const data = JSON.parse(fs.readFileSync(0, "utf8"));
const state = view.defaultState(data.charts.currency);
process.stdout.write(JSON.stringify({
  sources: view.toSources(data), defaultState: state, phoneState: view.stackForPhone(state),
  moneyState: view.moneyStateHtml([{ state: "Settled", amount: 300 }, { state: "Disputed", amount: 100 }, { state: "Waiting", amount: 0 }]),
  assistant: data.assistant && {
    events: view.turnEvents(data.assistant.reply), response: view.turnResponse(data.assistant.reply),
    body: view.turnBody(data.assistant.request), targets: data.assistant.states.map(view.configureTarget),
    panels: (data.assistant.panels || []).map(view.panelsForAssistant),
  },
}));
