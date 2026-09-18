"""De-identification profiles: every option has a fixture-backed test, the default profile reproduces the validated
policy byte for byte, and verify understands what each profile promised."""
import json
import subprocess
import sys
from pathlib import Path

import pydicom
import pytest

from scrubdicom.profiles import (BUILTIN, Profile, age_bucket_5y, age_years, load_profile, shift_da, shift_days_for, shift_dt)

CLI = [sys.executable, "-W", "ignore", "-m", "scrubdicom"]


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    d = tmp_path_factory.mktemp("fx")
    subprocess.run([sys.executable, "-m", "scrubdicom.fixtures", str(d)], check=True, capture_output=True)
    return d


def run_with(fixtures, tmp_path, profile: Profile | None, name="out"):
    out = tmp_path / name
    cmd = CLI + ["run", "--input", str(fixtures / "ORFAN0231"), "--output", str(out), "--study-id", "P-001", "--salt", "fixed-test-salt"]
    if profile is not None:
        pp = tmp_path / f"{name}_profile.json"
        profile.save(pp)
        cmd += ["--profile", str(pp)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    cts = [pydicom.dcmread(str(p)) for p in (out / "P-001").rglob("*.dcm")]
    assert cts
    return out, cts, r.stdout


def verify(out, extra=()):
    return subprocess.run(CLI + ["verify", "--output", str(out), *extra], capture_output=True, text=True)


# ------------------------------------------------------------------ pure helpers

def test_profile_defaults_and_serialisation(tmp_path):
    p = Profile()
    assert p.is_default() and p.kept_summary() == [] and p.method_string("9.9.9") == "Scrub-DICOM 9.9.9 full blind"
    q = Profile(keep_sex=True, shift_dates=True, method_text="")
    assert not q.is_default() and q.method_string("1.0.0") == "Scrub-DICOM 1.0.0 kept:sex,dates-shifted"
    assert len(Profile(method_text="x" * 100).method_string("1")) == 64
    path = q.save(tmp_path / "p.json")
    assert load_profile(path) == q
    assert Profile.from_dict({"keep_sex": "yes", "bogus": 1, "name": "N"}) == Profile(name="N"), "wrong types and unknown keys ignored"
    assert Profile(patient_name="custom").validate() and not Profile(patient_name="custom", patient_name_text="X").validate()
    assert Profile(study_description="a\\b").validate()
    with pytest.raises(SystemExit):
        load_profile(tmp_path / "missing.json")
    assert set(BUILTIN) == {"blinded", "longitudinal", "characteristics", "technical"} and BUILTIN["blinded"].is_default()


def test_date_shift_helpers():
    k = shift_days_for("salt", "P-001")
    assert 1 <= k <= 3650 and k == shift_days_for("salt", "P-001") and k != shift_days_for("salt", "P-002")
    assert shift_da("20190522", 1, "X") == "20190521" and shift_da("2019", 1, "X") == "X" and shift_da("", 1, "X") == "X"
    assert shift_dt("20190522154522.12", 22, "X") == "20190430154522.12" and shift_dt("abc", 1, "X") == "X"


def test_age_helpers():
    ds = pydicom.Dataset()
    ds.PatientAge = "058Y"
    assert age_years(ds) == 58 and age_bucket_5y(58) == "055Y" and age_bucket_5y(124) == "120Y"
    ds = pydicom.Dataset()
    ds.PatientBirthDate, ds.StudyDate = "19610314", "20190313"
    assert age_years(ds) == 57
    ds.StudyDate = "20190314"
    assert age_years(ds) == 58
    assert age_years(pydicom.Dataset()) is None


# ------------------------------------------------------------------ engine behaviour per option

def test_default_profile_matches_no_profile(fixtures, tmp_path):
    _, a, _ = run_with(fixtures, tmp_path, None, "a")
    _, b, _ = run_with(fixtures, tmp_path, Profile(), "b")
    strip = lambda ds: {e.keyword: str(e.value) for e in ds if e.VR not in ("OB", "OW", "UI") and e.keyword not in ("", "PixelData")}
    assert strip(a[0]) == strip(b[0])
    assert a[0].DeidentificationMethod.endswith("full blind") and "PatientSex" not in a[0] and a[0].StudyDate == "19000101"


def test_keep_sex_age_weight(fixtures, tmp_path):
    out, cts, _ = run_with(fixtures, tmp_path, BUILTIN["characteristics"])
    ds = cts[0]
    assert ds.PatientSex == "M" and ds.PatientAge == "055Y" and str(ds.PatientWeight) == "82"
    assert "PatientBirthDate" not in ds and ds.StudyDate == "19000101", "DOB still gone, dates still dummied"
    assert "kept:sex,age5y,weight" in ds.DeidentificationMethod
    assert (out / "_logs" / "profile.json").exists()
    v = verify(out)
    assert v.returncode == 0 and "PASS" in v.stdout, v.stdout
    # the same output judged by the strict default profile fails on the retained sex and age
    strict = tmp_path / "strict.json"
    Profile().save(strict)
    v2 = verify(out, ["--profile", str(strict)])
    assert v2.returncode == 1 and "PatientSex" in v2.stdout and "PatientAge" in v2.stdout


def test_shift_dates_keeps_intervals_and_hides_real_dates(fixtures, tmp_path):
    out, cts, _ = run_with(fixtures, tmp_path, BUILTIN["longitudinal"])
    ds = cts[0]
    assert ds.StudyDate != "20190522" and ds.StudyDate != "19000101"
    assert ds.StudyDate == ds.SeriesDate == ds.AcquisitionDate == ds.ContentDate, "one offset per patient"
    assert ds.AcquisitionDateTime.startswith(ds.StudyDate) and ds.AcquisitionDateTime.endswith("154522.120000")
    assert ds.ContrastBolusStartTime == "154025.48", "clock times survive when dates are shifted"
    assert all(str(x.StudyDate) == str(ds.StudyDate) for x in cts), "every file of the patient agrees"
    # inside sequences too
    assert ds.RequestAttributesSequence[0].ScheduledProcedureStepStartDate == ds.StudyDate if "RequestAttributesSequence" in ds else True
    v = verify(out, ["--needle", "20190522"])
    assert v.returncode == 0 and "PASS" in v.stdout, v.stdout
    strict = tmp_path / "strict.json"
    Profile().save(strict)
    assert "real date" in verify(out, ["--profile", str(strict)]).stdout


def test_keep_scanner_and_technical(fixtures, tmp_path):
    out, cts, _ = run_with(fixtures, tmp_path, BUILTIN["technical"])
    ds = cts[0]
    assert ds.Manufacturer == "SIEMENS" and ds.ManufacturerModelName == "SOMATOM Force" and ds.ConvolutionKernel == "B26f"
    assert str(ds.get("InstitutionName", "")) == "" and "DeviceSerialNumber" not in ds, "serial and institution still gone"
    v = verify(out)
    assert v.returncode == 0 and "PASS" in v.stdout, v.stdout


def test_institution_names_and_texts(fixtures, tmp_path):
    prof = Profile(name="custom", keep_institution=True, patient_name="custom", patient_name_text="CASE^A", study_description="CTCA", method_text="My method")
    out, cts, _ = run_with(fixtures, tmp_path, prof)
    ds = cts[0]
    assert ds.InstitutionName == "Example Hospital" and str(ds.PatientName) == "CASE^A" and ds.PatientID == "P-001"
    assert ds.StudyDescription == "CTCA" and ds.DeidentificationMethod == "My method"
    v = verify(out)
    assert v.returncode == 0 and "PASS" in v.stdout, v.stdout
    anon_out, anon, _ = run_with(fixtures, tmp_path, Profile(patient_name="anonymous"), "anon")
    assert str(anon[0].PatientName) == "ANONYMOUS" and "PASS" in verify(anon_out).stdout


def test_profiles_never_keep_the_floor(fixtures, tmp_path):
    """Whatever is ticked, private tags, UIDs, physicians, accession, comments and addresses go."""
    prof = Profile(name="everything", keep_sex=True, keep_age_5y=True, keep_weight_height=True, shift_dates=True,
                   keep_manufacturer=True, keep_technical=True, keep_institution=True)
    _, cts, _ = run_with(fixtures, tmp_path, prof)
    ds = cts[0]
    assert not any(e.tag.is_private for e in ds)
    assert not str(ds.StudyInstanceUID).startswith("1.3.12")
    for kw in ("ReferringPhysicianName", "OperatorsName", "AccessionNumber", "PhysiciansOfRecord"):
        assert str(ds.get(kw, "")) == ""
    for kw in ("PatientAddress", "ImageComments", "OtherPatientIDs", "IssuerOfPatientID", "DeviceSerialNumber", "SoftwareVersions", "PatientBirthDate"):
        assert kw not in ds
    assert "SendingApplicationEntityTitle" not in ds.file_meta


def test_profile_recorded_in_logs(fixtures, tmp_path):
    out, _, stdout = run_with(fixtures, tmp_path, BUILTIN["longitudinal"])
    assert "Profile: Longitudinal follow-up" in stdout
    assert json.loads((out / "_logs" / "profile.json").read_text())["shift_dates"] is True
