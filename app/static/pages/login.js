document.getElementById("login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget, button = form.querySelector("button[type=submit]"), msg = document.getElementById("msg");
  const email = form.email.value.trim(), password = form.password.value;
  if (!email || !password) { msg.textContent = "Enter your email and password."; return; }
  button.disabled = true; button.textContent = "Logging in…"; msg.textContent = "";
  try {
    const res = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }),
    });
    if (res.ok) { location.href = "/seller"; return; }
    const body = await res.json().catch(() => ({}));
    msg.textContent = res.status === 429 ? "Too many attempts, try again later."
      : body.detail && typeof body.detail === "string" ? body.detail : "Email or password is incorrect.";
    form.password.value = ""; form.password.focus();
  } catch { msg.textContent = "Could not reach the server."; }
  finally { button.disabled = false; button.textContent = "Log in"; }
});
