document.getElementById("setup").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget, button = f.querySelector("button[type=submit]"), msg = document.getElementById("msg");
  const body = { code: f.code.value.trim(), email: f.email.value.trim(), password: f.password.value, confirm: f.confirm.value };
  if (!body.code || !body.email || !body.password) { msg.textContent = "Fill in every field."; return; }
  if (body.password.length < 12) { msg.textContent = "Use at least 12 characters."; return; }
  if (body.password !== body.confirm) { msg.textContent = "The passwords don't match."; return; }
  button.disabled = true; button.textContent = "Creating…"; msg.textContent = "";
  try {
    const res = await fetch("/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (res.ok) { location.href = "/seller"; return; }
    if (res.status === 404) { location.href = "/login"; return; } // an account exists now
    const out = await res.json().catch(() => ({}));
    msg.textContent = res.status === 429 ? "Too many attempts, try again later."
      : typeof out.detail === "string" ? out.detail : "Couldn't create the account.";
  } catch { msg.textContent = "Could not reach the server."; }
  finally { button.disabled = false; button.textContent = "Create account"; }
});
