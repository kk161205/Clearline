const esc = UI.esc;
const $ = (id) => document.getElementById(id);
let url = `${location.origin}/mcp`;
const ROLE = { buyer: '<span class="pill plain">Buyer agent</span>', merchant: '<span class="pill settled plain">Merchant agent</span>' };
const time = (t) => UI.dateTime(t); // as on every other page
// A tool name may break after "_", never inside a word.
const tool = (name) => `<code class="tool">${esc(name).replace(/_/g, "_<wbr>")}</code>`;
$("url").textContent = url;
$("copy").addEventListener("click", () => UI.copy(url));

// "refused: rate limited" -> "Refused: rate limited"; outcomes are already plain words.
const outcome = (o) => `<span class="${o.startsWith("refused") ? "refused" : o === "error" ? "error" : ""}">${esc(o.charAt(0).toUpperCase() + o.slice(1))}</span>`;

async function load() {
  const res = await fetch("/api/agents");
  if (res.status === 401) { location.href = "/login"; return; }
  if (!res.ok) return;
  const d = await res.json();
  url = new URL(d.mcp_url, location.origin).href;
  $("url").textContent = url;
  $("transport").textContent = d.transport;
  $("sdk").textContent = `Official MCP Python SDK (${d.sdk.replace(/^mcp /, "version ")})`;
  $("merchant").innerHTML = d.merchant_enabled ? '<span class="pill settled">On</span>'
    : '<span class="pill">Off</span> <span class="muted small">Create an agent key in <a href="/seller#settings">Settings, Agent access</a> to turn them on.</span>';
  $("tools").innerHTML = d.tools.map((t) => `<tr><td data-label="Tool">${tool(t.name)}</td>
    <td data-label="For">${ROLE[t.role] || esc(t.role)}</td><td data-label="" class="desc wide">${esc(t.description)}</td></tr>`).join("");
  $("calls").innerHTML = d.calls.length ? d.calls.map((c) => `<tr><td data-label="Time" class="num">${esc(time(c.at))}</td>
    <td data-label="Tool">${tool(c.tool)}</td><td data-label="Agent">${ROLE[c.role] || esc(c.role)}</td>
    <td data-label="Result">${outcome(c.outcome)}</td><td data-label="Order">${c.order_ref ? `…${esc(c.order_ref)}` : '<span class="muted">None</span>'}</td></tr>`).join("")
    : '<tr><td colspan="5" data-label="" class="wide"><div class="empty"><strong>No agent calls yet</strong>Connect an agent with the URL above and ask it about an order.</div></td></tr>';
}
load();
setInterval(() => load().catch(() => {}), 5000);
