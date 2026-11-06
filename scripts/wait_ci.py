"""After a push: wait for the GitHub Actions run on HEAD, print each job and the version Render serves.

    python scripts/wait_ci.py
"""
import subprocess, time, httpx
from datetime import datetime as D
sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
api = "https://api.github.com/repos/kk161205/Clearline/actions/runs"
p = lambda t: D.fromisoformat(t.replace("Z", "+00:00"))
run = None
for _ in range(60):
    runs = httpx.get(api, params={"per_page": 5}, timeout=20).json()["workflow_runs"]
    run = next((r for r in runs if r["head_sha"] == sha), None)
    if run and run["status"] == "completed":
        break
    time.sleep(10)
print(run["html_url"], run["status"], run["conclusion"])
for j in httpx.get(f"{api}/{run['id']}/jobs", timeout=20).json()["jobs"]:
    print(" ", j["name"], j["conclusion"], (p(j["completed_at"]) - p(j["started_at"])).seconds if j.get("completed_at") else "-", "s")
print("live:", httpx.get("https://clearline-cbyb.onrender.com/healthz", timeout=60).headers.get("x-app-version"), "head:", sha[:7])
