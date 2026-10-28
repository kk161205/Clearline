// Runs the dashboard's real <script> (with ui.js and history.js) in node and renders the
// KPI tiles, grid rows and every row's side panel from an /api/dashboard payload on stdin:
// payload -> {kpis: html, rows: [gridRow], drawers: [html]}
const fs = require("fs");
const path = require("path");

const page = process.argv[2], dir = path.dirname(page);
const html = fs.readFileSync(page, "utf8");
global.document = { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] }; // no readyState: start() doesn't run
global.location = { origin: "https://shop.example" };
global.setInterval = () => 0;
new Function(fs.readFileSync(path.join(dir, "ui.js"), "utf8"))();
const script = [fs.readFileSync(path.join(dir, "history.js"), "utf8"), html.match(/<script>([\s\S]*)<\/script>/)[1]].join(";\n");
const view = new Function(script + "; return { kpiTiles, toGridRows, drawerHtml };")();

const data = JSON.parse(fs.readFileSync(0, "utf8"));
const rows = view.toGridRows(data.rows);
process.stdout.write(JSON.stringify({ kpis: view.kpiTiles(data.kpis), rows, drawers: rows.map(view.drawerHtml) }));
