"""scripts/secret_scan.py: every kind of secret is caught, and the repo itself is clean.
The samples are assembled at run time, so this file doesn't flag itself."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("secret_scan", ROOT / "scripts/secret_scan.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

SAMPLES = {
    "Neon host": "ep-cool-rain-123-pooler.eu-central-1.aws.neon" + ".tech",
    "Postgres URL with credentials": "postgres" + "ql://owner:s3cret@db.example.com/main",
    "Clearline agent key": "cl_live" + "_" + "a1B2" * 8,
    "PayPal client id or secret": "E" + "x9_-" * 19 + "abc",
    "PayPal access token": "A21" + "AA" + "b7" * 30,
    "Google API key": "AI" + "za" + "c3" * 17 + "d",
    "Groq API key": "gs" + "k_" + "e5" * 26,
    "Private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
}


@pytest.mark.parametrize("kind, sample", SAMPLES.items())
def test_each_kind_is_caught(kind, sample):
    assert kind in scan.hits(f'value = "{sample}"')


@pytest.mark.parametrize("line", [
    "postgres" + "ql://localhost/test",                  # no password
    "assert use_api_key('cl_live" + "_abcd')",            # a test's short prefix
    'integrity="sha384-7PJhP4EokYImcTjoBC8tU70g15uKpbbnOmciR0SmXBL5r5EBAqIVCo748JuYDB5n"',
    "A short sentence about Neon and PayPal.",
])
def test_ordinary_lines_are_not_flagged(line):
    assert scan.hits(line) == []


def test_the_repo_is_clean():
    assert scan.scan_tree() == []


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


def test_a_secret_added_and_removed_is_still_caught(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init")
    (tmp_path / "a.txt").write_text("nothing\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "first")
    (tmp_path / "a.txt").write_text(SAMPLES["Google API key"] + "\n")
    git(tmp_path, "commit", "-am", "oops")
    (tmp_path / "a.txt").write_text("nothing\n")
    git(tmp_path, "commit", "-am", "removed")
    assert scan.main([]) == 0  # the tree alone looks fine
    assert scan.main(["--diff", "HEAD~2", "HEAD"]) == 1
    out = capsys.readouterr().out
    assert "a.txt (diff): Google API key" in out and SAMPLES["Google API key"] not in out


def test_staged_lines_are_scanned_and_values_never_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init")
    (tmp_path / "b.txt").write_text(SAMPLES["Private key"] + "\n")
    git(tmp_path, "add", ".")
    assert scan.main(["--staged"]) == 1
    assert "BEGIN" not in capsys.readouterr().out
