"""End-to-end: build synthetic patients with planted identifiers, run, verify, and check the output by hand."""
import csv, re, subprocess, sys
from pathlib import Path
import pydicom
import pytest

CLI = [sys.executable, "-W", "ignore", "-m", "scrubdicom"]


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    d = tmp_path_factory.mktemp("fx")
    subprocess.run([sys.executable, "-m", "scrubdicom.fixtures", str(d)], check=True, capture_output=True)
    return d


def dcm_files(root):
    for p in Path(root).rglob("*.dcm"):
        yield p, pydicom.dcmread(str(p), force=True)


def test_run_and_verify(fixtures, tmp_path):
    out = tmp_path / "out"
    r = subprocess.run(CLI + ["run", "--input", str(fixtures), "--output", str(out), "--mapping", str(fixtures / "mapping_by_folder.csv"),
                              "--match-on", "folder"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (out / "CBB0401").is_dir() and (out / "CBB0402").is_dir()
    assert "ORFAN0999" in r.stdout                      # mapping row with no scans is reported
    cts = [(p, ds) for p, ds in dcm_files(out) if ds.Modality == "CT"]
    assert cts
    for p, ds in cts:
        assert ds.PatientName == ds.PatientID and str(ds.PatientID).startswith("CBB04")
        assert "PatientBirthDate" not in ds and "PatientSex" not in ds
        assert not any(e.tag.is_private for e in ds)
        for e in ds:
            if e.VR == "DA":
                assert e.value in ("", "19000101"), (p, e)
        assert not str(ds.StudyInstanceUID).startswith("1.3.12"), "original UID kept"
        assert str(ds.get("Manufacturer", "")) == "" and str(ds.get("InstitutionName", "")) == ""
        assert "SendingApplicationEntityTitle" not in ds.file_meta
    # dose SR quarantined, not in the reader tree
    assert list((out / "_review").rglob("*.dcm"))
    assert not [p for p, ds in dcm_files(out / "CBB0401") if ds.Modality == "SR"]
    # verify passes
    v = subprocess.run(CLI + ["verify", "--output", str(out), "--needle", "SMITH", "--needle", "BLOGGS"], capture_output=True, text=True)
    assert v.returncode == 0 and "PASS" in v.stdout, v.stdout
    # originals untouched
    names = {str(ds.PatientName) for _, ds in dcm_files(fixtures)}
    assert "SMITH^JOHN" in names


def test_manifest_resume_and_disconnect_safe(fixtures, tmp_path):
    out = tmp_path / "out2"
    m = tmp_path / "m.csv"
    m.write_text(f"source_folder,study_id\n{fixtures/'ORFAN0231'},CBB0501\n{fixtures/'nope'},CBB0502\n")
    r = subprocess.run(CLI + ["run", "--manifest", str(m), "--output", str(out), "--resume"], capture_output=True, text=True)
    assert r.returncode == 0 and (out / "CBB0501" / ".complete").exists() and "folder not found" in r.stdout
    r2 = subprocess.run(CLI + ["run", "--manifest", str(m), "--output", str(out), "--resume"], capture_output=True, text=True)
    assert "already complete" in r2.stdout


def test_dry_run_creates_nothing(fixtures, tmp_path):
    out = tmp_path / "out3"
    subprocess.run(CLI + ["run", "--input", str(fixtures), "--output", str(out), "--mapping", str(fixtures / "mapping.csv"), "--dry-run"],
                   check=True, capture_output=True)
    assert not out.exists()
