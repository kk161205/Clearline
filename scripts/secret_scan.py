"""Fail on anything that looks like a secret, in the repo's tracked files and in a git diff.

    python scripts/secret_scan.py                     every tracked file
    python scripts/secret_scan.py --diff BASE HEAD    also the lines those commits added
    python scripts/secret_scan.py --staged            also the staged lines (before a commit)

Prints the file, line and kind of each hit, never the matched text: a scan log is public.
"""

import argparse
import re
import subprocess
import sys

# The second part of the scheme is assembled so this file doesn't flag itself.
PATTERNS = {
    "Neon host": re.compile(r"[a-z0-9-]+\.neon\.tech\b", re.I),
    "Postgres URL with credentials": re.compile("postgres" r"(?:ql)?://[^\s:/@'\"]+:[^\s/@'\"]+@"),
    "Clearline agent key": re.compile(r"\bcl_live_[A-Za-z0-9_-]{16,}"),
    "PayPal client id or secret": re.compile(r"(?<![A-Za-z0-9_-])[AE][A-Za-z0-9_-]{79}(?![A-Za-z0-9_-])"),
    "PayPal access token": re.compile(r"\bA21AA[A-Za-z0-9_-]{40,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Groq API key": re.compile(r"\bgsk_[A-Za-z0-9]{40,}\b"),
    "Private key": re.compile("-----BEGIN " r"(?:[A-Z0-9]+ )*PRIVATE KEY-----"),
}
# Third-party minified code and generated long strings, where random text is normal.
SKIPPED = ("app/static/vendor/", "app/static/fonts/")


def hits(text: str) -> list[str]:
    """The kinds of secret found in one line."""
    return [kind for kind, pattern in PATTERNS.items() if pattern.search(text)]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout


def scan_tree() -> list[str]:
    found = []
    for path in git("ls-files", "-z").split("\0"):
        if not path or path.startswith(SKIPPED):
            continue
        try:
            with open(path, encoding="utf-8") as file:
                lines = file.readlines()
        except (OSError, UnicodeDecodeError):  # binary, or deleted but not staged
            continue
        found += [f"{path}:{number}: {kind}" for number, line in enumerate(lines, 1) for kind in hits(line)]
    return found


def scan_diff(*revisions: str) -> list[str]:
    """The added lines of `git diff`. A commit range also catches a secret added then removed."""
    found, path = [], "?"
    for line in git("diff", "--unified=0", "--no-color", *revisions).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and not path.startswith(SKIPPED):
            found += [f"{path} (diff): {kind}" for kind in hits(line)]
    return found


def scan_commits(base: str, head: str) -> list[str]:
    """Each commit in the range on its own, so a later commit can't hide an earlier secret."""
    found = []
    for commit in git("rev-list", f"{base}..{head}").split():
        found += [f"{commit[:7]} {hit}" for hit in scan_diff(f"{commit}^", commit)]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan the repo for secrets.")
    parser.add_argument("--diff", nargs=2, metavar=("BASE", "HEAD"))
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args(argv)
    found = scan_tree()
    if args.diff:
        found += scan_commits(*args.diff)
    if args.staged:
        found += scan_diff("--cached")
    for hit in found:
        print(hit)
    print(f"{len(found)} possible secret(s) found." if found else "No secrets found.")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
