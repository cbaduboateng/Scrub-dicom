"""The one-page PDF certificate: a valid file, the right facts, and never an original identifier. No display needed."""
import re
import subprocess
import sys

import pytest

from scrubdicom import demo_data
from scrubdicom.app import certificate, intake, model
from scrubdicom.app.model import JobSpec


def run_cli(args):
    return subprocess.run([sys.executable, "-W", "ignore", "-m", "scrubdicom", *args], capture_output=True, text=True)


@pytest.fixture(scope="module")
def verified(tmp_path_factory):
    t = tmp_path_factory.mktemp("cert")
    demo_data.main(t / "scans")
    it = intake.scan_folder(t / "scans")
    intake.assign_ids(it, "DEMO")
    out, conf = t / "out", t / "conf"
    spec = JobSpec(mode="manifest", manifest=str(intake.run_list(conf, it)), output=str(out), confidential=str(conf), ctca_only=True)
    assert run_cli(["run", *spec.run_args()]).returncode == 0
    v = run_cli(["verify", *model.verify_args(str(out), ["SMITH", "JONES"], False, "", str(conf))])
    assert "\nPASS" in v.stdout, v.stdout + v.stderr
    return out, conf


def check_structure(data: bytes) -> None:
    assert data.startswith(b"%PDF-1.4\n") and data.rstrip().endswith(b"%%EOF")
    start = int(re.search(rb"startxref\n(\d+)\n", data).group(1))
    assert data[start:start + 4] == b"xref"
    offsets = [int(m) for m in re.findall(rb"\n(\d{10}) 00000 n ", data[start:])]
    assert len(offsets) == 8
    for n, off in enumerate(offsets, 1):
        assert data[off:].startswith(b"%d 0 obj\n" % n), f"object {n} is not where the table says"
    length = int(re.search(rb"/Length (\d+)", data).group(1))
    body = data.split(b"stream\n", 1)[1]
    assert body[length:length + 10] == b"\nendstream", "the stream length is exact"
    assert data.count(b"/Type /Page ") == 1, "one page"


def test_certificate_of_a_verified_output(verified):
    out, _conf = verified
    path = certificate.write_certificate(out)
    assert path and path.parent == out / "_logs" and path.name.startswith("certificate_") and path.suffix == ".pdf"
    data = path.read_bytes()
    check_structure(data)
    text = data.decode("latin-1")
    for expected in ("(De-identification certificate)", "(Verified: no identifiers found)", "(PASS)", "DEMO-001, DEMO-002", "(Nothing)",
                     "240, every one re-read", "checksums_", "SHA-256 ", "(Checked by)", "\\(the words themselves are not recorded\\)"):
        assert expected in text, expected
    for secret in ("SMITH", "JONES", "1234567", "7654321", "Example Hospital", "19610314", str(out.parent)):
        assert secret not in text, f"the certificate must not carry {secret!r}"
    assert len(data) < 20_000
    # it is part of the hand-over, not a confidential log, and does not disturb the checksum re-check
    assert path not in model.confidential_log_files(out / "_logs")
    assert all(c.level == "ok" for c in model.share_readiness(out))
    assert run_cli(["verify", *model.recheck_args(str(out))]).returncode == 0


def test_no_certificate_without_a_pass(tmp_path, verified):
    assert certificate.write_certificate(tmp_path) is None
    (tmp_path / "_logs").mkdir()
    assert certificate.certificate_for(tmp_path) is None
    out, _ = verified
    # a newer failed check outdates the attestation: no certificate
    newer = out / "_logs" / "verify_99991231_235959.txt"
    newer.write_text("FAIL: 1 problem\n")
    try:
        assert certificate.certificate_for(out) is None
    finally:
        newer.unlink()
    assert certificate.certificate_for(out) is not None


def test_kept_things_long_id_lists_and_held_back_files():
    att = {"version": "9.9.9", "verified_at": "2026-10-02T14:30:00", "profile": {"retains": ["sex", "age5y"]},
           "output": {"patients": 300, "patients_marked_complete": 298, "files_verified": 123456},
           "verify": {"result": "PASS", "needles_used": 0}, "checksums": {"file": "checksums_x.sha256", "sha256": "ab" * 32},
           "statement": "Every file was re-read (and found clean). Pixel data was not inspected."}
    data = certificate.certificate_pdf(att, [f"SCAD-{i:03d}" for i in range(1, 301)], held_back=3, attestation_name="attestation_x.json")
    check_structure(data)
    text = data.decode("latin-1")
    assert "(Sex, a 5-year age band)" in text and "3, in _review" in text
    more = int(re.search(r"\(and (\d+) more\)", text).group(1))
    shown = len(re.findall(r"SCAD-\d{3}", text))
    assert shown + more == 300 and 30 < shown < 80, "the list says how many were left out, and the two add up"
    assert "298 complete" in text and "123,456" in text and "2 October 2026 at 14:30" in text
    assert "\\(beyond" in text, "brackets in text are escaped"
    assert "needle" not in text and "\x95" in text, "plain words; a real bullet character"
    assert "SCAD-300" not in text, "a long list is cut, not run off the page"


def test_wrapping():
    assert certificate.wrap("one two three", 10, 1000) == ["one two three"]
    lines = certificate.wrap("a" * 64, 8, 100)
    assert len(lines) > 1 and "".join(lines) == "a" * 64, "a hash is broken across lines, not lost"
    assert certificate.text_width("W", 10) > certificate.text_width("i", 10)
