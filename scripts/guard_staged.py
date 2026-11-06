"""Before a commit: the repo's secret scan on the staged diff, plus every value set in .env
(app settings and the E2E_ logins). Prints setting names only, never a value.

    python scripts/guard_staged.py
"""
import subprocess, sys
sys.path.insert(0, ".")
from app.config import Settings
from app.e2e import Config
added = "\n".join(l for l in subprocess.run(["git", "diff", "--cached", "-U0"], capture_output=True, text=True, encoding="utf-8").stdout.splitlines() if l.startswith("+"))
s, c = Settings(), Config()
vals = {n: getattr(s, n, None) for n in ("paypal_client_id", "paypal_client_secret", "paypal_webhook_id", "gemini_api_key", "groq_api_key", "app_secret", "database_url", "ag_studio_license_key")}
vals.update(seller_email=c.seller_email, seller_password=c.seller_password, buyer_email=c.buyer_email, buyer_password=c.buyer_password)
hits = [n for n, v in vals.items() if v and len(str(v)) >= 6 and str(v) in added]
scan = subprocess.run([sys.executable, "scripts/secret_scan.py", "--staged"])
print("env + E2E guard:", hits or "clean")
sys.exit(1 if hits or scan.returncode else 0)
