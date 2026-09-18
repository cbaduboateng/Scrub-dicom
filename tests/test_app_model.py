"""Unit tests for the desktop app's logic layer (scrubdicom.app.model). No display needed."""
import json
import sys
from pathlib import Path

import pytest

from scrubdicom.app import model
from scrubdicom.app.model import JobSpec, Settings


# ------------------------------------------------------------------ command building

def test_manifest_spec_maps_every_field_to_a_flag(tmp_path):
    m = tmp_path / "m.csv"
    m.write_text("source_folder,study_id\n/a,S1\n")
    pk = tmp_path / "p.csv"
    pk.write_text("study_id,series_uid,series_description\n")
    spec = JobSpec(mode="manifest", output=str(tmp_path / "out"), manifest=str(m), remap="/Volumes/D=E:\\", series_pick=str(pk),
                   ctca_only=True, resume=True, keep_technical=True, flat=True, dry_run=True, confidential=str(tmp_path / "conf"))
    assert spec.validate() == []
    args = spec.run_args()
    assert args == ["--manifest", str(m), "--remap", "/Volumes/D=E:\\", "--resume", "--ctca-only", "--series-pick", str(pk),
                    "--output", str(tmp_path / "out"), "--keep-technical", "--flat", "--confidential", str(tmp_path / "conf"), "--dry-run"]
    assert spec.command()[-len(args):] == args and "run" in spec.command()


def test_single_folder_spec(tmp_path):
    inp = tmp_path / "in"
    inp.mkdir()
    spec = JobSpec(mode="single", output=str(tmp_path / "out"), input=str(inp), study_id="STUDY-017", ctca_only=True, resume=True, confidential=str(tmp_path / "conf"))
    assert spec.validate() == []
    args = spec.run_args()
    assert args == ["--input", str(inp), "--study-id", "STUDY-017", "--output", str(tmp_path / "out"), "--confidential", str(tmp_path / "conf")]
    assert "--ctca-only" not in args and "--resume" not in args, "manifest-only options must not leak into single-folder mode"


def test_mapping_spec(tmp_path):
    inp = tmp_path / "in"
    inp.mkdir()
    mp = tmp_path / "ids.xlsx"
    mp.write_bytes(b"")
    spec = JobSpec(mode="mapping", output=str(tmp_path / "out"), input=str(inp), mapping=str(mp), current_col="Hospital ID",
                   new_col="Study ID", sheet="Sheet1", match_on="folder", confidential=str(tmp_path / "conf"))
    assert spec.validate() == []
    assert spec.run_args() == ["--input", str(inp), "--mapping", str(mp), "--current-col", "Hospital ID", "--new-col", "Study ID",
                               "--sheet", "Sheet1", "--match-on", "folder", "--output", str(tmp_path / "out"), "--confidential", str(tmp_path / "conf")]


def test_validation_catches_the_usual_mistakes(tmp_path):
    inp = tmp_path / "in"
    inp.mkdir()
    assert "Choose an output folder." in JobSpec(mode="manifest", manifest="x").validate()
    assert any("Manifest not found" in p for p in JobSpec(mode="manifest", output="o", manifest=str(tmp_path / "nope.csv")).validate())
    assert any("OLDPREFIX=NEWPREFIX" in p for p in JobSpec(mode="manifest", output="o", manifest=__file__, remap="bad").validate())
    assert any("study id" in p.lower() for p in JobSpec(mode="single", output="o", input=str(inp)).validate())
    assert any("only contain" in p for p in JobSpec(mode="single", output="o", input=str(inp), study_id="bad id!").validate())
    assert any("must not be the input" in p for p in JobSpec(mode="single", output=str(inp), input=str(inp), study_id="S").validate())
    assert any("inside the input" in p for p in JobSpec(mode="single", output=str(inp / "out"), input=str(inp), study_id="S").validate())
    assert any("mapping file" in p.lower() for p in JobSpec(mode="mapping", output="o", input=str(inp)).validate())
    assert any("confidential folder" in p.lower() for p in JobSpec(mode="single", output="o", input=str(inp), study_id="S").validate())
    assert any("outside the output" in p for p in JobSpec(mode="single", output=str(tmp_path / "o"), input=str(inp), study_id="S", confidential=str(tmp_path / "o" / "c")).validate())
    assert any("outside the output" in p for p in JobSpec(mode="single", output=str(tmp_path / "o"), input=str(inp), study_id="S", confidential=str(tmp_path)).validate())
    assert model.suggest_confidential("/x/Anon").endswith("Anon_CONFIDENTIAL") and model.recheck_args("/x") == ["--output", "/x", "--recheck"]
    assert model.verify_args("/out", [], False, "", "/conf") == ["--output", "/out", "--confidential", "/conf"]


def test_verify_and_thick_args():
    assert model.verify_args("/out", ["Smith", "", " Jones "], True) == ["--output", "/out", "--needle", "Smith", "--needle", "Jones", "--keep-technical"]
    assert model.thick_args("/out", False) == ["--output", "/out"]
    assert model.thick_args("/out", True) == ["--output", "/out", "--fix"]
    assert model.split_needles("Smith, Jones;Bloggs\n  RWE ") == ["Smith", "Jones", "Bloggs", "RWE"]


def test_engine_command_from_source_and_frozen(monkeypatch):
    assert model.engine_command(["verify", "--output", "x"]) == [sys.executable, "-u", "-W", "ignore", "-m", "scrubdicom", "verify", "--output", "x"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert model.engine_command(["run"]) == [sys.executable, "--cli", "run"]


# ------------------------------------------------------------------ progress and log lines

def test_parse_progress():
    p = model.parse_progress("[12/520] CBB0412 <- H0001234: 224 files, 1 series  (2%, ~310 min left), 40 non-CTCA images dropped")
    assert (p.done, p.total, p.study, p.percent, p.eta_minutes) == (12, 520, "CBB0412", 2, 310)
    assert "12 of 520" in p.text and "310 min" in p.text
    assert model.parse_progress("  CBB0401: 250 files...") is None
    assert model.parse_progress("[3/3] CBB0403: folder not found: /x").eta_minutes is None


def test_line_classification():
    assert model.is_skipped_line("[2/9] CBB0402: already complete, skipped (--resume)")
    assert model.classify_line("** Output drive disappeared **") == "error"
    assert model.classify_line("[1/2] X: folder not found: /x") == "error"
    assert model.classify_line("[4/9] CBB0404 <- F: 0 files  ** NO CORONARY SERIES FOUND (3 series) - skipped **") == "warn"
    assert model.classify_line("PASS: no residual identifiers") == "ok"
    assert model.classify_line("Done in 3.2 min: 9 studies") == "ok"
    assert model.classify_line("[5/9] CBB0405 <- G: 224 files, 1 series") == "plain"


# ------------------------------------------------------------------ output tree readers

def make_tree(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    logs = out / "_logs"
    logs.mkdir(parents=True)
    (out / "S1").mkdir()
    (out / "S1" / ".complete_ctca").write_text("t")
    (out / "S2").mkdir()  # half-finished
    (logs / "uid_salt.txt").write_text("abc")
    (logs / "series_20260101_000000.csv").write_text(
        "study_id,series_number,original_description,images,decision,reason\n"
        "S1,3,Cardiac 0.75 B26f,224,keep,coronary recon: 0.75 mm\n"
        "S1,1,Topogram,1,drop,localiser\n"
        "S2,5,Series 5,300,keep,coronary recon - no contrast agent [kept as fallback - CHECK]\n")
    (logs / "series_20260102_000000.csv").write_text(
        "study_id,series_number,original_description,images,decision,reason\n"
        "S1,3,Cardiac 0.75 B26f,224,keep,analysed series (FAI pick)\n")
    (logs / "files_20260102_000000.csv").write_text("study_id,source_path,output_path,status,note\n")
    (logs / "summary_20260102_000000.csv").write_text("study_id,source_folder,files,series,review_files,errors,status\nS1,/x/H123,224,1,0,0,OK\n")
    (logs / "LINKAGE_20260102_000000_CONFIDENTIAL.csv").write_text("study_id,source_folder,original_patient_id\nS1,/x/H123,1234567\n")
    return out


def test_study_state_and_series_rows(tmp_path):
    out = make_tree(tmp_path)
    assert model.study_state(out) == (["S1"], ["S2"])
    rows = model.load_series_rows(out / "_logs")
    assert len(rows) == 3, "the newer run replaces the older row for the same study/series"
    s1_3 = next(r for r in rows if r["study_id"] == "S1" and r["series_number"] == "3")
    assert s1_3["reason"].startswith("analysed series") and s1_3["run"] == "20260102_000000"
    assert [r["study_id"] for r in model.filter_series(rows, "Needs a look")] == ["S2"]
    assert len(model.filter_series(rows, "Kept")) == 2 and len(model.filter_series(rows, "Dropped")) == 1
    assert len(model.filter_series(rows, "All", "s2")) == 1
    assert model.series_counts(rows) == {"studies": 2, "kept": 2, "dropped": 1, "check": 1}


def test_manifest_count(tmp_path):
    m = tmp_path / "m.csv"
    m.write_text("source_folder,study_id\n/a,S1\n,S2\n/c,\n/d,S4\n")
    assert model.manifest_count(str(m)) == 2
    assert model.manifest_count(str(tmp_path / "none.csv")) is None
    assert model.manifest_count("") is None


def test_verify_status(tmp_path):
    out = make_tree(tmp_path)
    logs = out / "_logs"
    assert model.verify_status(logs) == (None, None, "")
    (logs / "verify_20260101_000000.txt").write_text("Verified 3 files\n\nFAIL: 1 findings\n")
    assert model.verify_status(logs)[0] == "FAIL"
    (logs / "verify_20260103_000000.txt").write_text("Verified 3 files\n\nPASS: no residual identifiers\n")
    status, rep, _ = model.verify_status(logs)
    assert status == "PASS" and rep.name.endswith("20260103_000000.txt")


# ------------------------------------------------------------------ sharing

def test_share_readiness_blocks_until_verified_and_linkage_removed(tmp_path):
    out = make_tree(tmp_path)
    levels = {c.title: c.level for c in model.share_readiness(out)}
    assert levels["Not verified"] == "block"
    assert any(t.startswith("1 linkage file") and lvl == "block" for t, lvl in levels.items())
    assert any("half-finished" in t and lvl == "warn" for t, lvl in levels.items())
    assert any("other log file" in t and lvl == "warn" for t, lvl in levels.items())
    (out / "_logs" / "verify_20260109_000000.txt").write_text("PASS: ok\n")
    moved, folder = model.move_logs_out(out, tmp_path / "safe")
    assert not (out / "_logs" / "uid_salt.txt").exists(), "the salt goes with the linkage material"
    assert any(p.name == "uid_salt.txt" for p in moved)
    assert not list((out / "_logs").glob("LINKAGE_*")) and not list((out / "_logs").glob("files_*"))
    assert folder.parent == (tmp_path / "safe").resolve() and "CONFIDENTIAL" in folder.name
    assert {p.name for p in moved} >= {"LINKAGE_20260102_000000_CONFIDENTIAL.csv", "files_20260102_000000.csv", "summary_20260102_000000.csv"}
    levels = {c.title: c.level for c in model.share_readiness(out)}
    assert "Verify passed" in levels and levels["No linkage file in the output tree"] == "ok"
    assert not any(lvl == "block" for lvl in levels.values())


def test_move_logs_refuses_destination_inside_output(tmp_path):
    out = make_tree(tmp_path)
    with pytest.raises(ValueError):
        model.move_logs_out(out, out / "somewhere")
    with pytest.raises(ValueError):
        model.move_logs_out(out, out)


def test_redact_paths_and_error_log(tmp_path, monkeypatch):
    txt = 'FileNotFoundError: /Users/someone/Scans/H1234567/IM-0001.dcm and C:\\Scans\\H1234567\\x.dcm in "/a/b/c"'
    red = model.redact_paths(txt)
    assert "H1234567" not in red and "<path>" in red
    monkeypatch.setattr(model, "settings_path", lambda: tmp_path / "settings.json")
    try:
        raise ValueError("boom /Users/x/secret")
    except ValueError as e:
        import sys
        p = model.record_error(*sys.exc_info())
    assert p == tmp_path / "errors.log" and "boom <path>" in p.read_text() and "/Users/x" not in p.read_text()


def test_linkage_files_found_anywhere_in_tree(tmp_path):
    out = make_tree(tmp_path)
    (out / "S1" / "LINKAGE_x_CONFIDENTIAL.csv").write_text("x")
    assert len(model.linkage_files(out)) == 2


# ------------------------------------------------------------------ settings

def test_settings_persist_only_allowlisted_keys(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings.load(p)
    s.set("match_on", "folder")
    s.set("ctca_only", False)
    s.set("output", "/hospital/H1234567")   # paths are not allow-listed: never persisted
    s.set("needles", "SMITH")               # not allow-listed: silently ignored
    s.values["study_id"] = "STUDY-1"        # even a direct write must not be persisted
    s.save()
    raw = json.loads(p.read_text())
    assert raw["match_on"] == "folder" and raw["ctca_only"] is False
    assert "needles" not in raw and "study_id" not in raw and "output" not in raw and "H1234567" not in p.read_text()
    assert set(raw) <= model.SETTINGS_KEYS
    again = Settings.load(p)
    assert again.get("match_on") == "folder" and again.get("ctca_only") is False
    assert again.spec().output == "" and again.spec().match_on == "folder"


def test_settings_ignore_wrong_types_and_bad_json(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"ctca_only": "yes", "sheet": 5, "mode": "manifest", "unknown": 1}')
    s = Settings.load(p)
    assert s.get("ctca_only") is True and s.get("sheet") == ""
    p.write_text("not json")
    assert Settings.load(p).get("mode") == "manifest"


def test_spec_to_settings_skips_study_id(tmp_path):
    s = Settings.load(tmp_path / "s.json")
    model.spec_to_settings(JobSpec(mode="single", output="/o", input="/i", study_id="X", match_on="folder"), s)
    assert s.get("match_on") == "folder" and "study_id" not in s.values and "output" not in s.values


def test_external_viewer_command(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert model.external_viewer_command(Path("/x/f.dcm"), "/Applications/Bee DICOM Viewer.app") == ["open", "-a", "/Applications/Bee DICOM Viewer.app", "/x/f.dcm"]
    assert model.external_viewer_command(Path("/x"), "") == ["open", "/x"]
    assert model.viewer_app_name("/Applications/Bee DICOM Viewer.app") == "Bee DICOM Viewer"
    assert model.viewer_app_name("") == "system default viewer"
    assert "external_viewer" in model.SETTINGS_KEYS


def test_find_applications(tmp_path):
    (tmp_path / "Bee DICOM Viewer.app").mkdir()
    (tmp_path / "Zed.app").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "Fake.app").write_text("not a bundle")
    apps = model.find_applications([tmp_path, tmp_path / "missing"])
    assert [n for n, _ in apps] == ["Bee DICOM Viewer", "Zed"]
