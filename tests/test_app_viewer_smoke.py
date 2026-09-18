"""Drive the viewer window through its code paths on the synthetic patients: load a patient, select a series, render,
step slices, zoom, reformat, window presets, header diff, then output mode with quarantine release. Skipped when no
display is available (headless CI)."""
import subprocess
import sys
import time
import tkinter as tk

import pytest

from scrubdicom.app import preview as pv
from scrubdicom.app.model import Settings


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    d = tmp_path_factory.mktemp("fx")
    subprocess.run([sys.executable, "-m", "scrubdicom.fixtures", str(d)], check=True, capture_output=True)
    out = tmp_path_factory.mktemp("out")
    m = tmp_path_factory.mktemp("m") / "patients.csv"
    m.write_text(f"source_folder,study_id\n{d / 'ORFAN0231'},CBB0701\n{d / 'ORFAN0418'},CBB0702\n")
    subprocess.run([sys.executable, "-m", "scrubdicom.app", "--cli", "run", "--manifest", str(m), "--output", str(out)], check=True, capture_output=True)
    return d, out, m


@pytest.fixture
def app(data, tmp_path):
    try:
        from scrubdicom.app.ui import App
        a = App()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    a.settings = Settings.load(tmp_path / "settings.json")
    d, out, m = data
    a.v_mode.set("manifest")
    a._apply_mode()
    a.v_manifest.set(str(m))
    a.v_output.set(str(out))
    a.update()
    yield a
    a.destroy()


def pump(widget, seconds=6.0, until=None):
    t0 = time.time()
    while time.time() - t0 < seconds:
        widget.update()
        if until and until():
            return True
        time.sleep(0.05)
    return bool(until and until())


def test_viewer_source_mode_end_to_end(app, data):
    from scrubdicom.app.viewer import ViewerWindow
    w = ViewerWindow(app, "source", "CBB0701")
    assert pump(w, until=lambda: len(w.series) == 3), w.v_status.get()
    assert w.v_patient.get() == "CBB0701" and w._study_id() == "CBB0701"
    # a CT series is selected and rendered
    w.tv.selection_set("1")
    pump(w, 0.5)
    assert w.cur is not None and w.cur.modality == "CT" and w.view is not None and w.photo is not None
    assert w.canvas.find_all(), "something drawn on the canvas"
    # header diff shows the engine's changes
    rows = w.th.get_children("")
    assert rows and any(w.th.set(r, "name") == "PatientName" and w.th.set(r, "act") == "changed" for r in rows)
    assert "removed" in w.v_summary.get()
    # slices, zoom, presets, invert, hover readout
    n0 = w.idx
    w._step(1)
    assert w.idx == min(n0 + 1, w.cur.n_images - 1)
    w._zoom_step(1)
    assert w.zoom > 1.0 and w.lbl_zoom.cget("text") == f"{w.zoom * 100:.0f}%"
    w.v_zoom_pct.set(250)
    w._zoom_from_slider()
    assert abs(w.zoom - 2.5) < 1e-6
    w._set_zoom(1.0, reset_pan=True)
    assert abs(float(w.v_zoom_pct.get()) - 100) < 1e-6
    # drag tools: zoom and pan
    class P: x, y = 300, 300
    class Q: x, y = 300, 200
    w.v_tool.set("Zoom"); w._press(P); w._motion(Q); w._mouse_release(Q)
    assert w.zoom > 1.0
    w._set_zoom(1.0, reset_pan=True)
    w.v_tool.set("Pan"); w._press(P); w._motion(Q); w._mouse_release(Q)
    assert w.pan == [0.0, -100.0]
    w._set_zoom(1.0, reset_pan=True)
    w.v_tool.set("Window/Level")
    assert any("kV" in str(w.canvas.itemcget(i, "text")) for i in w.canvas.find_all() if w.canvas.type(i) == "text"), "kVp in the annotations"
    w.v_preset.set("Coronary (300 / 800)")
    w._preset()
    assert w.wl == (300.0, 800.0)
    w.v_invert.set(True)
    w._render()
    class E: x, y = int(w.off[0] + 10 * w.scale), int(w.off[1] + 10 * w.scale)
    w._hover(E)
    assert w.v_readout.get().startswith("x ")
    # thumbnails arrive
    assert pump(w, until=lambda: len(w.thumbs) >= 2)
    # reformats
    w.v_orient.set("Coronal")
    w._orient()
    assert pump(w, until=lambda: w.vol is not None), w.v_status.get()
    assert w.view is not None and w.view.shape[0] == w.cur.n_images
    w.v_orient.set("Axial")
    w._orient()
    # cine
    w._toggle_play()
    pump(w, 0.4)
    assert w.playing
    w._toggle_play()
    assert not w.playing
    # series override written next to the manifest, then removed
    w._override()
    pump(w, 0.3)
    picks = pv.default_picks_path(str(data[2]))
    assert picks.exists() and "CBB0701" in picks.read_text()
    assert app.v_series_pick.get() == str(picks)
    w._unoverride()
    assert "CBB0701" not in picks.read_text()
    w.destroy()


def test_viewer_output_mode_quarantine(app, data, monkeypatch):
    from scrubdicom.app import viewer as vw
    _, out, _ = data
    w = vw.ViewerWindow(app, "output", "CBB0701")
    assert pump(w, until=lambda: any(s.verdict == "review" for s in w.series)), w.v_status.get()
    review_idx = next(i for i, s in enumerate(w.series) if s.verdict == "review")
    w.tv.selection_set(str(review_idx))
    pump(w, 0.5)
    assert w.cur.verdict == "review"
    assert "disabled" not in w.b_release.state(), "release enabled for quarantined series"
    # the SR has no pixels: the canvas explains instead of crashing
    assert w.view is None and any("Cannot display" in str(w.canvas.itemcget(i, "text")) for i in w.canvas.find_all())
    # header of the anonymised file, no diff
    assert w.th.get_children("") and "Header of the anonymised" in w.lbl_header.cget("text")
    # release unchanged (no boxes) after confirming the dialog
    monkeypatch.setattr(vw.messagebox, "askyesno", lambda *a, **k: True)
    before = len(list((out / "_review" / "CBB0701").glob("*.dcm")))
    w._do_release(False)
    assert pump(w, until=lambda: len(list((out / "_review" / "CBB0701").glob("*.dcm"))) == before - 1)
    assert list((out / "_logs").glob("redactions_*.csv"))
    w.destroy()
