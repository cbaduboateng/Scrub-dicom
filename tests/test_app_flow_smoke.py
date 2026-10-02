"""Drive the main window through the folder-first flow on the demo patients: open a folder, choose kinds of series,
choose what to keep, rename, and check the engine command the window builds. Skipped when no display is available."""
import gc
import time
import tkinter as tk
from pathlib import Path

import pytest

from scrubdicom import demo_data
from scrubdicom.app import intake, model

SAVE_STEP, DONE_STEP = 3, 4
from scrubdicom.app.model import Settings


@pytest.fixture
def scans(tmp_path):
    d = tmp_path / "flow" / "scans"
    demo_data.main(d)
    return d


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "settings_path", lambda: tmp_path / "appdata" / "settings.json")
    try:
        from scrubdicom.app import ui
        a = ui.App(offer_demo=False)
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    a.settings = Settings.load(tmp_path / "settings.json")
    dialogs = []
    for name in ("showerror", "showinfo", "askokcancel", "askyesno"):
        monkeypatch.setattr(ui.messagebox, name, lambda title="", message="", _n=name, **k: dialogs.append((_n, title, message)) or True)
    a.dialogs = dialogs
    yield a
    a.destroy()
    a = None
    # Finalise this window's Tk variables here, on the main thread. Left for later, the collector may run inside a
    # worker thread of the next test, where each variable waits a second for a main loop that tests never start.
    gc.collect()


def pump(a, seconds=60.0, until=None):
    t0 = time.time()
    while time.time() - t0 < seconds:
        a.update()
        if until and until():
            return True
        time.sleep(0.03)
    return bool(until and until())


def test_nothing_is_possible_before_a_folder_is_opened(app):
    # the first screen asks one question and shows nothing else
    app._goto_step(0)
    app.update()
    assert app._problems_by_step()[0] == ["Say whether this is one patient or several."]
    assert not app.f_folder.winfo_ismapped() and not app.cb_listway.winfo_ismapped() and not app.f_list.winfo_ismapped()
    app.v_cases.set("one")
    app._apply_way()
    app.update()
    assert app.f_folder.winfo_ismapped() and not app.cb_listway.winfo_ismapped(), "one patient: a folder button, no talk of lists"
    assert app.b_open.cget("text") == "Choose the patient's folder..."
    assert app._problems_by_step()[0] == ["Choose the folder that holds the scans."]
    app.v_cases.set("many")
    app._apply_way()
    app.update()
    assert app.cb_listway.winfo_ismapped() and app.b_open.cget("text") == "Choose the folder of patients..."
    assert app.b_start.instate(["disabled"]) and app._top_next[0].instate(["disabled"])
    assert not app._materialise()


def test_one_patient_chosen_but_the_folder_holds_several(app, scans):
    app._goto_step(0)
    app.v_cases.set("one")
    app._apply_way()
    app._open_folder(str(scans), wait=True)
    app.update()
    msg = app._problems_by_step()[0]
    assert msg and "holds 2 patients, not one" in msg[0] and app.v_found.get() == msg[0]
    assert app.b_switch.winfo_ismapped() and app.b_start.instate(["disabled"]) and app._top_next[0].instate(["disabled"])
    app._switch_to_many()
    app.update()
    assert not app._problems_by_step()[0] and not app.b_switch.winfo_ismapped() and app.v_found.get().startswith("Found 2 patients")
    assert app.b_start.instate(["!disabled"])
    # and the right folder for one patient: a single ID box, not a table
    app.v_cases.set("one")
    app._apply_way()
    app._open_folder(str(scans / "SMITH_JOHN_1234567"), wait=True)
    app._goto_step(SAVE_STEP)
    app.update()
    assert not app._problems_by_step()[0] and app.f_one.winfo_ismapped() and not app.f_many.winfo_ismapped()
    app.v_one_id.set("Case 12")
    assert app.intake.units[0].new_id == "Case 12" and all(not s for s in app._problems_by_step())


def test_open_folder_fills_every_step_and_suggests_safe_destinations(app, scans):
    app._open_folder(str(scans), wait=True)
    it = app.intake
    assert it and len(it.units) == 2 and app.v_found.get().startswith("Found 2 patients")
    assert app.v_cases.get() == "many", "opened without answering the question: the folder answers it"
    assert len(app.tv_found.get_children("")) == 2 and len(app.tv_kinds.get_children("")) == len(app.kinds) == 6
    assert app.v_choice.get() == "coronary" and [u.new_id for u in it.units] == ["ANON-001", "ANON-002"]
    out, conf = app.v_output.get(), app.v_confidential.get()
    assert Path(out).parent == scans.parent and "SMITH" not in out and conf == out + "_CONFIDENTIAL"
    assert not Path(conf).exists(), "nothing is written until a run, a preview or the viewer needs it"
    assert all(not s for s in app._problems_by_step()), app._problems_by_step()
    assert app.b_start.instate(["!disabled"]), "the scan is the preview: no dry run needed first"
    spec = app._spec()
    assert spec.mode == "manifest" and spec.ctca_only and spec.series_select == "" and spec.manifest.endswith("this_run_patient_list.csv")
    assert "2 patients" in app.v_summary.get() and "coronary CT only (2 series, 240 images)" in app.v_summary.get()


def test_choosing_kinds_and_keeping_things(app, scans):
    app._open_folder(str(scans), wait=True)
    app.v_choice.set("choose")
    app._choice_changed()
    assert app.ticked == {app.kinds[0].key} and "Keeps 2 series, 240 images" in app.v_plan.get()
    app._tick_all(False)
    assert app._problems_by_step()[1] and app.b_start.instate(["disabled"])
    ca = next(k for k in app.kinds if k.description.startswith("CaScore"))
    app.ticked = {ca.key}
    app._fill_kinds()
    app._live_validate()
    spec = app._spec()
    assert not spec.ctca_only and spec.series_select.endswith(intake.SELECTION)
    # what to keep: ticks become an ordinary profile file; the banner says so in plain words
    app.v_strip_choice.set("custom")
    app.v_keep["keep_sex"].set(True)
    app.v_keep["keep_age_5y"].set(True)
    app._strip_changed()
    prof = model.profile_for_path(app.v_profile.get())
    assert prof.keep_sex and prof.keep_age_5y and not prof.shift_dates
    assert "sex" in app.v_banner.get() and "5-year age band" in app.v_banner.get() and "age5y" not in app.v_banner.get()
    app.v_strip_choice.set("default")
    app._strip_changed()
    assert app.v_profile.get() == ""
    # files appear only now
    assert app._materialise()
    conf = Path(app.v_confidential.get())
    rows = (conf / intake.SELECTION).read_text().splitlines()
    assert len(rows) == 3 and all("CaScore" in r for r in rows[1:])
    assert (conf / "this_run_patient_list.csv").exists() and (conf / intake.PATIENT_LIST).exists()


def test_ids_prefix_rename_and_stability(app, scans):
    app._open_folder(str(scans), wait=True)
    app.v_prefix.set("SCAD")
    assert [u.new_id for u in app.intake.units] == ["SCAD-001", "SCAD-002"]
    app._rename(app.intake.units[1], "SCAD-001")
    app._live_validate()
    assert any("same new ID" in p for p in app._problems_by_step()[3]) and app.b_start.instate(["disabled"])
    app._rename(app.intake.units[1], "My Case 7")
    app._live_validate()
    assert not app._problems_by_step()[3]
    app.v_prefix.set("X")
    assert app.intake.units[1].new_id == "My Case 7", "a typed ID is not renumbered behind the user's back"
    assert app._materialise()
    # a list that was never run pins nothing: opening the folder again numbers from the prefix
    app._open_folder(str(scans), wait=True)
    assert [u.new_id for u in app.intake.units] == ["X-001", "X-002"]
    # once those patients are in the output, the same folder with the same destinations gives everyone the ID they had
    for u, nid in zip(app.intake.units, ("SCAD-001", "My Case 7")):
        app._rename(u, nid)
    assert app._materialise()
    out = Path(app.v_output.get())
    for name in ("SCAD-001", "My_Case_7"):
        (out / name).mkdir(parents=True)
        (out / name / ".complete").write_text("")
    app._open_folder(str(scans), wait=True)
    assert [u.new_id for u in app.intake.units] == ["SCAD-001", "My Case 7"]
    app._assign_ids(force=True)
    assert [u.new_id for u in app.intake.units] == ["SCAD-001", "My Case 7"], "Renumber never renames a patient already written"


def test_the_confirmation_is_a_few_plain_rows(app, scans):
    from scrubdicom.app import ui
    app._open_folder(str(scans), wait=True)
    heading, rows = app._confirm_rows()
    assert heading == "Anonymise 2 patients?"
    labels = [r[0] for r in rows]
    assert labels == ["From", "Keeping", "Removing", "Copies go to", "The key goes to"]
    by = {r[0]: r for r in rows}
    assert by["Keeping"][1] == "Coronary CT only: 2 series, 240 images" and by["Removing"][1] == "Everything that identifies"
    assert by["Copies go to"][1].startswith(".../") and by["Copies go to"][2] == app.v_output.get(), "short on screen, the full path in the tooltip"
    assert all(len(r[1]) < 60 for r in rows), "no row is a wall of text"
    # the window itself: built from those rows, Anonymise confirms, anything else does not
    win = ui.ConfirmRun(app, heading, rows, "Your original scans are not changed.")
    app.update()
    assert win.rows == [(r[0], r[1]) for r in rows] and str(win.b_ok.cget("text")) == "Anonymise"
    win.b_ok.invoke()
    assert win.result is True and not win.winfo_exists()
    win = ui.ConfirmRun(app, heading, rows)
    app.update()
    win.b_cancel.invoke()
    assert win.result is False
    # keeping something, and a study with nothing to keep, are both said
    app.v_strip_choice.set("custom")
    app.v_keep["keep_sex"].set(True)
    app._strip_changed()
    app.v_choice.set("choose")
    app._choice_changed()
    app.ticked = {next(k for k in app.kinds if k.modality == "US").key}
    _, rows = app._confirm_rows()
    by = {r[0]: r[1] for r in rows}
    assert by["Removing"] == "Everything that identifies, except sex" and by["Skipping"] == "1 with nothing to keep"
    app.v_strip_choice.set("default")
    app._strip_changed()
    # a real run asks; the answer decides
    asked = []
    app._confirm_run = lambda: asked.append(1) or False
    app.v_choice.set("coronary")
    app._choice_changed()
    app._start_run()
    assert asked == [1] and not (app.proc and app.proc.running), "declined: nothing starts"


def test_output_inside_the_scans_is_refused(app, scans):
    app._open_folder(str(scans), wait=True)
    app.v_output.set(str(scans / "out"))
    app._live_validate()
    assert any("must not be inside the folder of scans" in p for p in app._problems_by_step()[3])


def test_viewer_ticks_for_one_patient_win_and_can_be_cleared(app, scans):
    from scrubdicom.app.viewer import ViewerWindow
    app._open_folder(str(scans), wait=True)
    assert app._materialise()
    w = ViewerWindow(app, "source", "ANON-001")
    assert pump(app, 20, lambda: len(w.series) == 6), w.v_status.get()
    topo = next(s for s in w.series if s.description.startswith("Topogram"))
    w.ticked = {topo.uid}
    w._use_ticked()
    assert app.overrides == {"ANON-001": {topo.uid}} and app.step == SAVE_STEP
    spec = app._spec()
    assert spec.ctca_only and spec.series_select.endswith(intake.SELECTION), "the rule for everyone else, the ticks for this patient"
    assert "ticked in the viewer" in app.tv_ids.set("0", "keeps")
    app.ticks_cleared("ANON-001")
    assert app.overrides == {} and app._spec().series_select == ""


def test_demo_plays_to_a_verified_green_finish(app):
    app._run_demo(auto=True)
    assert pump(app, 150, lambda: app.demo_stage is None and not (app.proc and app.proc.running)), app.v_progress_text.get()
    out = app._out()
    assert model.verify_status(out / "_logs")[0] == "PASS"
    assert app.v_strip.get() == "Verified. Safe to hand over."
    # the journey ends on the results screen: one headline, the certificate written, the example of what was removed
    assert app.step == DONE_STEP and app.v_done_head.get() == "2 patients anonymised and verified"
    assert "240 files" in app.v_done_detail.get() and not app.f_issues.winfo_children()
    assert app.b_done_hand.instate(["!disabled"]) and app.b_done_cert.instate(["!disabled"]) and app.b_done_view.instate(["!disabled"])
    certs = list((out / "_logs").glob("certificate_*.pdf"))
    assert len(certs) == 1 and certs[0].read_bytes().startswith(b"%PDF")
    assert app.card_done.winfo_ismapped() and app.card_done.v_title.get().startswith("What was removed, shown on DEMO-00")
    assert not app.f_log.winfo_ismapped(), "the engine's log stays out of sight unless asked for"
    assert sum(1 for _ in (out / "DEMO-001").rglob("*.dcm")) == 120
    names = {p.name.split("_2")[0] for p in (out / "_logs").iterdir()}
    assert not any(n.startswith("app") for n in names), "the window's own run log goes to the confidential folder"
    assert not [d for d in app.dialogs if d[0] == "showerror"], app.dialogs


def test_list_driven_methods_are_still_there(app, tmp_path):
    m = tmp_path / "m.csv"
    m.write_text("source_folder,study_id\n/nowhere,S1\n")
    app.v_listway.set(True)
    app._apply_way()
    app.v_mode.set("manifest")
    app._apply_mode()
    app.v_manifest.set(str(m))
    app.v_output.set(str(tmp_path / "out"))
    app._live_validate()
    spec = app._spec()
    assert spec.mode == "manifest" and spec.manifest == str(m)
    assert app.rb_choice["choose"].instate(["disabled"]) and not app.f_kinds.winfo_ismapped()
    # only the two methods that really use a list are offered; a single patient is "Choose folder"
    assert set(app.tiles) == {"manifest", "mapping"}
    app._goto_step(0)
    app.update()
    assert all(app.tiles[m].winfo_ismapped() for m in app.tiles), "both are visible without hunting for them"
    app.v_mode.set("single")
    app._apply_mode()
    assert app.v_mode.get() == "manifest"
    app.v_mode.set("mapping")
    app._apply_mode()
    app.update()
    assert app.rows_cols[1].winfo_ismapped(), "the spreadsheet method shows how the old ID is matched without a hidden toggle"
    app.v_mode.set("manifest")
    app._apply_mode()
    app._live_validate()
    assert all(not s for s in app._problems_by_step())
    assert app.b_start.instate(["disabled"]) and app.b_dry.instate(["!disabled"]), "a list-driven run still wants a dry run first"
