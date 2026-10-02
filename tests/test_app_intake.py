"""The folder-first intake: scan any folder, work out studies and series, and write what the engine needs.
No display needed. Everything runs on synthetic patients."""
import csv
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scrubdicom import demo_data
from scrubdicom.app import intake
from scrubdicom.app.model import JobSpec
from scrubdicom.core import iter_dicom_files


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo") / "scans"
    n = demo_data.main(d)
    return d, n


def run_cli(args):
    return subprocess.run([sys.executable, "-W", "ignore", "-m", "scrubdicom", *args], capture_output=True, text=True)


# ------------------------------------------------------------------ scanning

def test_scan_finds_patients_series_and_every_file(demo):
    d, n = demo
    it = intake.scan_folder(d)
    assert not it.problems and not it.mixed
    assert len(it.units) == 2 and it.n_patients == 2
    assert it.n_files == n == sum(1 for _ in iter_dicom_files(d, None)) - 0, "the scan counts the same files the engine will read"
    assert [it.label(u) for u in it.units] == ["JONES_MARY_7654321", "SMITH_JOHN_1234567"]
    smith = it.units[1]
    assert smith.key == "1234567" and smith.folder == d / "SMITH_JOHN_1234567"
    by_desc = {s.description: s for s in smith.series}
    cta = by_desc["CorCTA 0.6 Bv40 BestDiast 75 %"]
    assert cta.verdict == "keep" and cta.n_images == demo_data.CTA_SLICES and cta.thickness == 0.6
    assert by_desc["CaScore 3.0 Qr36"].verdict == "drop" and by_desc["Topogram 0.6 T20f"].verdict == "drop"
    assert by_desc["Chest PA"].modality == "DX" and by_desc["Chest PA"].review, "the X-ray is held back for a look"
    assert "2 patients" in it.headline()


def test_sampled_and_unsampled_scans_agree(demo, monkeypatch):
    d, _ = demo
    a = intake.scan_folder(d)
    monkeypatch.setattr(intake, "SAMPLE_OVER", 10 ** 9)          # read every header
    b = intake.scan_folder(d)
    sig = lambda it: [(u.key, sorted((s.uid, s.n_images, s.verdict) for s in u.series)) for u in it.units]
    assert sig(a) == sig(b)


def test_scan_reports_progress_and_can_be_cancelled(demo):
    d, _ = demo
    seen = []
    intake.scan_folder(d, progress=lambda n, s: seen.append(n))
    assert seen and seen[-1] > 0
    it = intake.scan_folder(d, cancelled=lambda: True)
    assert it.cancelled and not it.units


def test_empty_and_missing_folders(tmp_path):
    assert intake.scan_folder(tmp_path).headline().startswith("No DICOM")
    assert intake.scan_folder(tmp_path / "nope").problems
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "junk.bin").write_bytes(b"\x00" * 300)
    it = intake.scan_folder(tmp_path)
    assert not it.units and it.n_unreadable == 1


def test_two_studies_of_one_patient_in_their_own_folders_get_an_id_each(demo, tmp_path):
    d, _ = demo
    root = tmp_path / "pairs"
    shutil.copytree(d / "SMITH_JOHN_1234567", root / "acute")
    # the convalescent scan: same patient, a different study
    import pydicom
    from pydicom.uid import generate_uid
    new_study = generate_uid()
    remap: dict[str, str] = {}
    for f in sorted((d / "SMITH_JOHN_1234567").rglob("*.dcm")):
        ds = pydicom.dcmread(str(f))
        ds.StudyInstanceUID = new_study
        ds.SeriesInstanceUID = remap.setdefault(str(ds.SeriesInstanceUID), generate_uid())
        ds.StudyDate = "20191120"
        out = root / "convalescent" / f.relative_to(d / "SMITH_JOHN_1234567")
        out.parent.mkdir(parents=True, exist_ok=True)
        ds.save_as(str(out))
    it = intake.scan_folder(root)
    assert len(it.units) == 2 and it.n_patients == 1 and not it.mixed
    assert sorted(it.label(u) for u in it.units) == ["acute", "convalescent"]
    assert "2 studies from 1 patients" in it.headline() or "2 studies" in it.headline()


def test_patients_sharing_one_folder_fall_back_to_patient_id(demo, tmp_path):
    d, _ = demo
    flat = tmp_path / "dump"
    flat.mkdir()
    for k, f in enumerate(sorted(d.rglob("*.dcm"))):
        shutil.copy(f, flat / f"{k:05d}.dcm")
    it = intake.scan_folder(flat)
    assert it.mixed and len(it.units) == 2 and {u.key for u in it.units} == {"1234567", "7654321"}
    assert all(u.folder == flat for u in it.units) and not it.problems
    assert it.label(it.units[0]).startswith("Patient ID ")


def test_an_earlier_output_inside_the_folder_is_not_read_as_input(demo, tmp_path):
    d, _ = demo
    root = tmp_path / "work"
    shutil.copytree(d / "SMITH_JOHN_1234567", root / "SMITH_JOHN_1234567")
    out = root / "Anonymised scans"
    (out / "_logs").mkdir(parents=True)
    shutil.copytree(d / "JONES_MARY_7654321" / "S005_CorCTA", out / "ANON-001" / "S005")
    (out / "ANON-001" / ".complete").write_text("")
    it = intake.scan_folder(root)
    assert len(it.units) == 1 and any("earlier anonymised output" in n for n in it.notes)


# ------------------------------------------------------------------ kinds and choices

def test_kinds_and_default_ticks(demo):
    it = intake.scan_folder(demo[0])
    kinds = intake.series_kinds(it)
    assert kinds[0].description.startswith("CorCTA") and kinds[0].coronary == 2 and kinds[0].note == "coronary CT"
    assert intake.default_ticks(kinds) == {kinds[0].key}
    dx = next(k for k in kinds if k.modality == "DX")
    assert dx.n_units == 2 and "burned-in" in dx.note
    us = next(k for k in kinds if k.modality == "US")
    assert us.n_units == 1
    assert next(k for k in kinds if k.description.startswith("CaScore")).slice_text == "3 mm"


def test_plan_for_each_choice(demo):
    it = intake.scan_folder(demo[0])
    intake.assign_ids(it)
    kinds = intake.series_kinds(it)
    cor = intake.plan(it, "coronary", set())
    assert (cor.n_units, cor.n_series, cor.n_images, cor.empty) == (2, 2, 2 * demo_data.CTA_SLICES, [])
    everything = intake.plan(it, "all", set())
    assert everything.n_series == it.n_series
    ca = {k.key for k in kinds if k.description.startswith("CaScore")}
    chosen = intake.plan(it, "choose", ca)
    assert chosen.n_series == 2 and chosen.n_images == 32
    us = {k.key for k in kinds if k.modality == "US"}
    only_us = intake.plan(it, "choose", us)
    assert only_us.n_units == 1 and len(only_us.empty) == 1 and "skipped" in only_us.text(2)
    assert "Nothing would be kept" in intake.plan(it, "choose", set()).text(2)
    # a study's own ticks from the viewer win over the cohort choice
    sid = it.units[0].new_id
    one = it.units[0].series[0].uid
    assert intake.plan(it, "coronary", set(), {sid: {one}}).n_series == 2
    assert [s.uid for s in intake.kept_series(it.units[0], "coronary", set(), {one})] == [one]


# ------------------------------------------------------------------ new IDs

def test_ids_are_numbered_and_stable_across_sessions(demo, tmp_path):
    it = intake.scan_folder(demo[0])
    intake.assign_ids(it, "SCAD")
    assert [u.new_id for u in it.units] == ["SCAD-001", "SCAD-002"] and intake.id_problems(it) == []
    conf = tmp_path / "conf"
    intake.write_patient_list(conf, it)
    # a later session: one study already known, and the output already holds SCAD-003 from somewhere else
    again = intake.scan_folder(demo[0])
    existing = intake.read_patient_list(conf)
    del existing[str(again.units[1].folder)]
    intake.assign_ids(again, "SCAD", existing, taken={"SCAD-002", "scad-003"})
    assert [u.new_id for u in again.units] == ["SCAD-001", "SCAD-004"], "known study keeps its ID; the new one never reuses a taken ID"


def test_id_problems(demo):
    it = intake.scan_folder(demo[0])
    intake.assign_ids(it, "")
    assert [u.new_id for u in it.units] == ["001", "002"]
    it.units[1].new_id = "001"
    assert any("same new ID" in p for p in intake.id_problems(it))
    it.units[1].new_id = "bad/id"
    assert any("may only contain" in p for p in intake.id_problems(it))
    it.units[1].new_id = " "
    assert any("needs a new ID" in p for p in intake.id_problems(it))


def test_ids_from_a_spreadsheet_match_patient_id_or_folder(demo):
    it = intake.scan_folder(demo[0])
    n = intake.apply_spreadsheet(it, {"1234567": "CBB0401", "JONES_MARY_7654321": "CBB0402", "UNKNOWN": "X"})
    assert n == 2 and {u.key: u.new_id for u in it.units} == {"1234567": "CBB0401", "7654321": "CBB0402"}


# ------------------------------------------------------------------ files for the engine

def test_selection_rows(demo):
    it = intake.scan_folder(demo[0])
    intake.assign_ids(it)
    kinds = intake.series_kinds(it)
    assert intake.selection_rows(it, "coronary", set()) == [] and intake.selection_rows(it, "all", set()) == []
    rows = intake.selection_rows(it, "choose", {kinds[0].key})
    assert len(rows) == 2 and {r[0] for r in rows} == {"ANON-001", "ANON-002"} and all(r[2].startswith("CorCTA") for r in rows)
    sid, uid = it.units[0].new_id, it.units[0].series[0].uid
    assert intake.selection_rows(it, "coronary", set(), {sid: {uid}}) == [(sid, uid, it.units[0].series[0].description)]


def test_selection_file_is_written_and_removed(tmp_path):
    p = intake.write_selection(tmp_path, [("A", "1.2.3", "x")])
    assert Path(p).read_text().splitlines() == ["study_id,series_uid,series_description", "A,1.2.3,x"]
    assert intake.write_selection(tmp_path, []) == "" and not Path(p).exists()


def test_suggested_folders_never_carry_the_source_name(tmp_path):
    src = tmp_path / "SMITH_JOHN_1234567"
    src.mkdir()
    out, conf = intake.suggest_folders(src)
    assert "SMITH" not in out and "SMITH" not in conf and "1234567" not in out
    assert Path(out).parent == tmp_path and conf == out + "_CONFIDENTIAL"
    spec = JobSpec(mode="manifest", manifest=__file__, output=out, confidential=conf)
    assert not [p for p in spec.validate() if "confidential" in p.lower()]
    # a place that cannot be written to: fall back to Documents
    out2, _ = intake.suggest_folders(Path("/"), documents=tmp_path / "Documents")
    assert out2.startswith(str(tmp_path / "Documents" / "Scrub-DICOM"))


# ------------------------------------------------------------------ the engine honours what the intake wrote

def test_coronary_only_run_from_an_opened_folder(demo, tmp_path):
    it = intake.scan_folder(demo[0])
    intake.assign_ids(it, "DEMO")
    out, conf = tmp_path / "out", tmp_path / "conf"
    lst = intake.run_list(conf, it)
    intake.write_patient_list(conf, it)
    spec = JobSpec(mode="manifest", manifest=str(lst), output=str(out), confidential=str(conf), ctca_only=True, resume=True)
    assert spec.validate() == []
    r = run_cli(["run", *spec.run_args()])
    assert r.returncode == 0, r.stdout + r.stderr
    for sid in ("DEMO-001", "DEMO-002"):
        files = list((out / sid).rglob("*.dcm"))
        assert len(files) == demo_data.CTA_SLICES, f"{sid}: only the coronary series is written"
    assert not (out / "_review").exists(), "the X-ray, dose report and ultrasound were dropped, not quarantined"
    v = run_cli(["verify", "--output", str(out), "--confidential", str(conf), "--needle", "SMITH", "--needle", "JONES"])
    assert "\nPASS" in v.stdout, v.stdout + v.stderr


def test_chosen_kinds_run_from_an_opened_folder(demo, tmp_path):
    it = intake.scan_folder(demo[0])
    intake.assign_ids(it, "DEMO")
    kinds = intake.series_kinds(it)
    ticked = {k.key for k in kinds if k.description.startswith("CaScore")}
    out, conf = tmp_path / "out", tmp_path / "conf"
    lst = intake.run_list(conf, it)
    sel = intake.write_selection(conf, intake.selection_rows(it, "choose", ticked))
    spec = JobSpec(mode="manifest", manifest=str(lst), output=str(out), confidential=str(conf), ctca_only=False, series_select=sel)
    r = run_cli(["run", *spec.run_args()])
    assert r.returncode == 0, r.stdout + r.stderr
    assert all(len(list((out / sid).rglob("*.dcm"))) == 16 for sid in ("DEMO-001", "DEMO-002")), "exactly the ticked kind"


def test_shared_folder_run_matches_on_patient_id(demo, tmp_path):
    flat = tmp_path / "dump"
    flat.mkdir()
    for k, f in enumerate(sorted(demo[0].rglob("*.dcm"))):
        shutil.copy(f, flat / f"{k:05d}.dcm")
    it = intake.scan_folder(flat)
    intake.assign_ids(it, "MIX")
    out, conf = tmp_path / "out", tmp_path / "conf"
    lst = intake.run_list(conf, it)
    with open(lst, newline="") as fh:
        assert next(csv.reader(fh)) == ["current_id", "new_id"]
    sel = intake.write_selection(conf, intake.selection_rows(it, "coronary", set()))
    assert sel, "with shared folders the coronary choice is spelled out series by series"
    spec = JobSpec(mode="mapping", input=str(flat), mapping=str(lst), match_on="patientid", output=str(out), confidential=str(conf), series_select=sel)
    assert spec.validate() == []
    r = run_cli(["run", *spec.run_args()])
    assert r.returncode == 0, r.stdout + r.stderr
    assert all(len(list((out / u.new_id).rglob("*.dcm"))) == demo_data.CTA_SLICES for u in it.units)
