"""Guards against the leaks that matter for this repo: real scans or linkage files committed, the app reaching the
network, telemetry left on, secrets in the tree. Cheap to run, so they run every time."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def tracked_files() -> list[Path]:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in r.stdout.decode().split("\0") if p]


def test_no_scans_logs_or_linkage_files_in_the_tree():
    bad = [p for p in tracked_files() if p.suffix.lower() == ".dcm" or p.name.upper().endswith("_CONFIDENTIAL.CSV")
           or "_logs" in p.parts or "_review" in p.parts or p.name.startswith("run_log")]
    assert not bad, f"these must never be committed: {bad}"


def test_gitignore_covers_the_confidential_patterns():
    gi = (ROOT / ".gitignore").read_text()
    for pat in ("*.dcm", "_logs/", "_review/", "*_CONFIDENTIAL.csv", ".venv/", "dist/", "build/"):
        assert pat in gi, pat


def test_app_never_touches_the_network():
    forbidden = re.compile(r"^\s*(import|from)\s+(socket|http|urllib|requests|ssl|ftplib|smtplib|webbrowser|xmlrpc|asyncio)\b", re.M)
    for p in (ROOT / "scrubdicom" / "app").glob("*.py"):
        assert not forbidden.search(p.read_text()), f"{p.name} imports a network module"
    core = (ROOT / "scrubdicom" / "core.py").read_text()
    assert not forbidden.search(core), "engine imports a network module"


def test_no_web_server_anywhere_in_the_package():
    forbidden = re.compile(r"\b(streamlit|flask|fastapi|tornado|http\.server|socketserver|uvicorn)\b")
    for p in (ROOT / "scrubdicom").rglob("*.py"):
        assert not forbidden.search(p.read_text()), f"{p} references a web server"


def test_no_secrets_or_real_identifiers_in_source():
    secret = re.compile(r"(api[_-]?key|secret|token|password)\s*[=:]\s*['\"][A-Za-z0-9/+_-]{12,}", re.I)
    nhs = re.compile(r"(?<![\d.])\d{3}[ -]?\d{3}[ -]?\d{4}(?![\d.])")     # NHS-number shape; not a run of digits inside a DICOM UID
    for p in tracked_files():
        if p.suffix not in (".py", ".md", ".txt", ".csv", ".toml", ".sh", ".bat", ".spec", ".iss") or not p.is_file():
            continue
        text = p.read_text(errors="replace")
        assert not secret.search(text), f"secret-like string in {p}"
        if p.name != "fixtures.py":  # the synthetic fixture plants an obviously fake NHS number on purpose
            assert not nhs.search(text), f"NHS-number-shaped string in {p}"


def test_settings_allowlist_has_no_identifier_or_path_fields():
    from scrubdicom.app.model import SETTINGS_KEYS
    for k in SETTINGS_KEYS:
        assert not re.search(r"needle|patient|name|dob|salt|id$|output|manifest|input|mapping|series_pick|remap", k), f"settings must not persist {k}"
