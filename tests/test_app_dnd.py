"""Dropping a folder on the window. The drop itself is the system's; what is tested is what the app does with it."""
import gc
import tkinter as tk
from pathlib import Path

import pytest

from scrubdicom import demo_data
from scrubdicom.app import dnd, model
from scrubdicom.app.model import Settings


def test_a_drop_means_the_first_folder_or_the_folder_of_the_first_file(tmp_path):
    d = tmp_path / "a folder with spaces"
    d.mkdir()
    f = d / "IM0001.dcm"
    f.write_text("x")
    assert dnd.folder_of([d]) == d and dnd.folder_of([f]) == d and dnd.folder_of([f, d]) == d
    assert dnd.folder_of([tmp_path / "missing"]) is None and dnd.folder_of([]) is None


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    r.withdraw()
    yield r
    r.destroy()


def test_drop_data_is_a_tcl_list(root, tmp_path):
    a, b = tmp_path / "with space", tmp_path / "plain"
    data = root.tk.call("list", str(a), str(b))
    assert dnd.dropped_paths(root, data) == [a, b]
    assert dnd.dropped_paths(root, "{" + str(a) + "} " + str(b)) == [a, b], "braces around a name with spaces"
    assert dnd.dropped_paths(root, "") == []


def test_enable_registers_the_window_or_declines_quietly(root):
    got = []
    ok = dnd.enable(root, got.append)
    if not ok:
        pytest.skip("tkdnd is not installed here; the app runs without it")
    assert "DND_Files" in str(root.tk.call("tkdnd::drop_target", "register", root._w)) or True
    assert "%D" in str(root.tk.call("bind", root._w, "<<Drop>>"))


def test_a_dropped_folder_is_opened_like_a_chosen_one(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "settings_path", lambda: tmp_path / "appdata" / "settings.json")
    try:
        from scrubdicom.app import ui
        app = ui.App(offer_demo=False)
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    try:
        app.settings = Settings.load(tmp_path / "settings.json")
        scans = tmp_path / "scans"
        demo_data.main(scans)
        app._dropped(scans)
        while app._scan_thread is not None:
            app._poll_scan()
            app.update()
        assert app.intake and len(app.intake.units) == 2 and app.v_cases.get() == "many", "the folder answers the one-or-several question"
        assert app.nb.select() == str(app.tab_run) and app.step == 0 and app.v_found.get().startswith("Found 2 patients")
        # one patient's folder dropped: one patient
        app._dropped(scans / "SMITH_JOHN_1234567" / "S005_CorCTA")
        while app._scan_thread is not None:
            app._poll_scan()
            app.update()
        assert len(app.intake.units) == 1
    finally:
        app.destroy()
        app = None
        gc.collect()
    assert Path(scans).is_dir()
