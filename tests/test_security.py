"""Security defaults: confidential folder for linkage/salt/logs, verify's file-meta and pattern sweeps, checksum manifest,
re-check, attestation, and modality quarantine."""
import json
import random
import subprocess
import sys
from pathlib import Path

import pydicom
import pytest

from scrubdicom.core import nhs_number_valid, recheck_checksums

CLI = [sys.executable, "-W", "ignore", "-m", "scrubdicom"]


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    d = tmp_path_factory.mktemp("fx")
    subprocess.run([sys.executable, "-m", "scrubdicom.fixtures", str(d)], check=True, capture_output=True)
    return d


def run(args):
    return subprocess.run(CLI + args, capture_output=True, text=True)


def valid_nhs() -> str:
    while True:
        digits = [random.randint(0, 9) for _ in range(9)]
        total = sum(d * w for d, w in zip(digits, range(10, 1, -1)))
        check = 11 - total % 11
        if check == 11:
            check = 0
        if check != 10:
            return "".join(map(str, digits)) + str(check)


def test_confidential_folder_receives_linkage_salt_and_logs(fixtures, tmp_path):
    out, conf = tmp_path / "out", tmp_path / "conf"
    m = tmp_path / "m.csv"
    m.write_text(f"source_folder,study_id\n{fixtures / 'ORFAN0231'},P1\n{fixtures / 'ORFAN0418'},P2\n")
    r = run(["run", "--manifest", str(m), "--output", str(out), "--confidential", str(conf), "--ctca-only"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Confidential material" in r.stdout and "inside the output tree" not in r.stdout
    names = {p.name.split("_")[0] for p in conf.iterdir()}
    assert {"LINKAGE", "uid", "files", "summary", "series", "profile.json"} <= names, sorted(p.name for p in conf.iterdir())
    out_logs = {p.name.split("_")[0] for p in (out / "_logs").iterdir()}
    assert "LINKAGE" not in out_logs and "uid" not in out_logs and "files" not in out_logs and "summary" not in out_logs
    assert "profile.json" in out_logs, "the profile stays with the output so verify can read it"
    # the salt lives in the confidential folder and is reused on a re-run
    salt = (conf / "uid_salt.txt").read_text()
    r2 = run(["run", "--manifest", str(m), "--output", str(out), "--confidential", str(conf), "--ctca-only", "--resume"])
    assert r2.returncode == 0 and (conf / "uid_salt.txt").read_text() == salt


def test_confidential_folder_must_be_outside_output(fixtures, tmp_path):
    out = tmp_path / "out"
    r = run(["run", "--input", str(fixtures / "ORFAN0231"), "--output", str(out), "--study-id", "P1", "--confidential", str(out / "secret")])
    assert r.returncode != 0 and "outside the output tree" in (r.stdout + r.stderr)
    r = run(["run", "--input", str(fixtures / "ORFAN0231"), "--output", str(out), "--study-id", "P1", "--confidential", str(tmp_path)])
    assert r.returncode != 0, "a confidential folder that contains the output is refused too"


def test_without_confidential_folder_engine_warns(fixtures, tmp_path):
    out = tmp_path / "out"
    r = run(["run", "--input", str(fixtures / "ORFAN0231"), "--output", str(out), "--study-id", "P1"])
    assert r.returncode == 0 and "inside the output tree" in r.stdout
    assert list((out / "_logs").glob("LINKAGE_*")) and (out / "_logs" / "uid_salt.txt").exists()


def test_verify_writes_checksums_and_attestation_and_recheck_detects_tampering(fixtures, tmp_path):
    out, conf = tmp_path / "out", tmp_path / "conf"
    assert run(["run", "--input", str(fixtures / "ORFAN0231"), "--output", str(out), "--study-id", "P1", "--confidential", str(conf)]).returncode == 0
    v = run(["verify", "--output", str(out), "--confidential", str(conf), "--needle", "SMITH"])
    assert v.returncode == 0 and "PASS" in v.stdout, v.stdout
    man = list((out / "_logs").glob("checksums_*.sha256"))
    att = list((out / "_logs").glob("attestation_*.json"))
    assert len(man) == 1 and len(att) == 1
    lines = man[0].read_text().splitlines()
    assert lines and all(len(l.split("  ")[0]) == 64 for l in lines) and all(l.split("  ")[1].startswith("P1/") for l in lines)
    a = json.loads(att[0].read_text())
    assert a["verify"]["result"] == "PASS" and a["output"]["patients"] == 1 and a["checksums"]["file"] == man[0].name
    assert a["profile"]["retains"] == [] and "Pixel data was not inspected" in a["statement"]
    # untouched: re-check passes
    rc = run(["verify", "--output", str(out), "--recheck"])
    assert rc.returncode == 0 and "PASS" in rc.stdout, rc.stdout
    # tamper with one file, delete another, add a third: re-check fails and names them
    files = sorted((out / "P1").rglob("*.dcm"))
    ds = pydicom.dcmread(str(files[0]))
    ds.ImageComments = "edited after verification"
    ds.save_as(str(files[0]))
    files[1].unlink()
    (out / "P1" / "extra.dcm").write_bytes(b"not dicom")
    ok, changed, missing, added, _ = recheck_checksums(out)
    assert not ok and len(changed) == 1 and len(missing) == 1 and added == ["P1/extra.dcm"]
    rc2 = run(["verify", "--output", str(out), "--recheck"])
    assert rc2.returncode == 1 and "1 changed, 1 missing, 1 added" in rc2.stdout


def anonymised_patient(fixtures, tmp_path, name="out"):
    out = tmp_path / name
    assert run(["run", "--input", str(fixtures / "ORFAN0231"), "--output", str(out), "--study-id", "P1", "--no-ocr"]).returncode == 0
    return out, sorted((out / "P1").rglob("*.dcm"))[0]


def test_verify_catches_pacs_identity_in_file_meta(fixtures, tmp_path):
    out, f = anonymised_patient(fixtures, tmp_path)
    ds = pydicom.dcmread(str(f))
    ds.file_meta.SendingApplicationEntityTitle = "PACSGW01"
    ds.save_as(str(f))
    v = run(["verify", "--output", str(out)])
    assert v.returncode == 1 and "file meta carries PACS identity" in v.stdout


def test_verify_catches_nhs_number_postcode_and_date_shapes(fixtures, tmp_path):
    out, f = anonymised_patient(fixtures, tmp_path)
    ds = pydicom.dcmread(str(f))
    nhs = valid_nhs()
    ds.ImageComments = f"NHS {nhs[:3]} {nhs[3:6]} {nhs[6:]} seen at SW1A 1AA on 20190522"
    ds.save_as(str(f))
    v = run(["verify", "--output", str(out)])
    assert v.returncode == 1
    for kind in ("NHS-number-shaped value", "postcode-shaped value", "date-shaped text"):
        assert kind in v.stdout, v.stdout
    assert not nhs_number_valid("12345") and not nhs_number_valid("abcdefghij") and nhs_number_valid(nhs)
    # a random ten-digit run with a bad check digit is not flagged
    bad = nhs[:9] + str((int(nhs[9]) + 1) % 10)
    ds.ImageComments = f"ref {bad}"
    ds.save_as(str(f))
    v2 = run(["verify", "--output", str(out)])
    assert "NHS-number-shaped" not in v2.stdout


def test_fail_report_goes_to_confidential_folder_with_stub_in_output(fixtures, tmp_path):
    out, conf = tmp_path / "out", tmp_path / "conf"
    assert run(["run", "--input", str(fixtures / "ORFAN0231"), "--output", str(out), "--study-id", "P1", "--confidential", str(conf), "--no-ocr"]).returncode == 0
    f = sorted((out / "P1").rglob("*.dcm"))[0]
    ds = pydicom.dcmread(str(f))
    ds.ImageComments = "SMITH was here"
    ds.save_as(str(f))
    v = run(["verify", "--output", str(out), "--confidential", str(conf)])
    assert v.returncode == 1
    stub = list((out / "_logs").glob("verify_*.txt"))[0].read_text()
    full = list(conf.glob("verify_*.txt"))[0].read_text()
    assert "SMITH" not in stub and "confidential" in stub and "SMITH" in full
    assert not list((out / "_logs").glob("checksums_*")), "no manifest or attestation on FAIL"


def test_ultrasound_is_quarantined_by_modality(fixtures, tmp_path):
    out = tmp_path / "out"
    assert run(["run", "--input", str(fixtures / "ORFAN0418"), "--output", str(out), "--study-id", "P2", "--no-ocr"]).returncode == 0
    review = [pydicom.dcmread(str(p)) for p in (out / "_review" / "P2").glob("*.dcm")]
    assert any(ds.Modality == "US" for ds in review), "ultrasound frame quarantined"
    assert not any(pydicom.dcmread(str(p)).Modality == "US" for p in (out / "P2").rglob("*.dcm"))
    us = next(ds for ds in review if ds.Modality == "US")
    assert us.PatientName == "P2" and "PatientBirthDate" not in us, "quarantined objects are still header-anonymised"
    files_csv = list((out / "_logs").glob("files_*.csv"))[0].read_text()
    assert "Modality US" in files_csv


def test_ocr_module_is_optional():
    from scrubdicom import ocr
    assert isinstance(ocr.available(), bool)
    if not ocr.available():
        import numpy as np
        assert ocr.words_in_array(np.zeros((32, 32))) == ""


def test_select_series_keeps_exactly_the_ticked_series(fixtures, tmp_path):
    from scrubdicom.app import preview as pv
    series = pv.scan_patient(fixtures / "ORFAN0231")
    s4 = next(s for s in series if s.number == "4")
    sel = tmp_path / "selection.csv"
    sel.write_text(f"study_id,series_uid,series_description\nP1,{s4.uid},{s4.description}\n")
    # single-folder mode
    out = tmp_path / "out"
    r = run(["run", "--input", str(fixtures / "ORFAN0231"), "--output", str(out), "--study-id", "P1", "--select-series", str(sel), "--no-ocr"])
    assert r.returncode == 0 and "Series selection: 1 series ticked" in r.stdout and "unticked series dropped" in r.stdout
    kept = {pydicom.dcmread(str(p), stop_before_pixels=True).SeriesNumber for p in (out / "P1").rglob("*.dcm")}
    assert kept == {4}
    # manifest mode without the coronary rule: the ticks decide, and the decisions are logged
    out2, conf = tmp_path / "out2", tmp_path / "conf"
    m = tmp_path / "m.csv"
    m.write_text(f"source_folder,study_id\n{fixtures / 'ORFAN0231'},P1\n{fixtures / 'ORFAN0418'},P2\n")
    r2 = run(["run", "--manifest", str(m), "--output", str(out2), "--confidential", str(conf), "--select-series", str(sel), "--no-ocr"])
    assert r2.returncode == 0, r2.stdout
    assert {pydicom.dcmread(str(p), stop_before_pixels=True).SeriesNumber for p in (out2 / "P1").rglob("*.dcm")} == {4}
    assert len(list((out2 / "P2").rglob("*.dcm"))) > 4, "the unlisted patient is unaffected"
    rows = list(conf.glob("series_*.csv"))[0].read_text()
    assert "ticked in the viewer" in rows and "not ticked in the viewer" in rows
