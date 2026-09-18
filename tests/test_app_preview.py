"""The viewer's logic layer (scrubdicom.app.preview) on the synthetic patients: series scan, in-memory header
diff, pixel loading and windowing, redaction + release from quarantine, and series overrides."""
import subprocess
import sys
from pathlib import Path

import numpy as np
import pydicom
import pytest

from scrubdicom.app import preview as pv
from scrubdicom.core import load_series_picks


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    d = tmp_path_factory.mktemp("fx")
    subprocess.run([sys.executable, "-m", "scrubdicom.fixtures", str(d)], check=True, capture_output=True)
    return d


@pytest.fixture(scope="module")
def anonymised(fixtures, tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    subprocess.run([sys.executable, "-m", "scrubdicom.app", "--cli", "run", "--input", str(fixtures / "ORFAN0231"), "--output", str(out),
                    "--study-id", "CBB0801"], check=True, capture_output=True)
    return out


def test_scan_patient_groups_and_classifies(fixtures):
    series = pv.scan_patient(fixtures / "ORFAN0231")
    assert [s.number for s in series] == ["3", "4", "501"]
    ct = series[0]
    assert ct.modality == "CT" and ct.n_images == 4 and ct.thickness == 0.75 and ct.kernel == "B26f"
    assert ct.verdict == "drop" and "only 4 images" in ct.reason, "fixture series are below the 100-image rule"
    sr = series[2]
    assert sr.modality == "SR" and sr.review and "SR" in sr.review
    assert all(p.suffix == ".dcm" for s in series for p in s.files)
    assert ct.files[0].name.endswith("-0001.dcm"), "files sorted by InstanceNumber"


def test_header_diff_shows_what_the_engine_does(fixtures):
    f = next((fixtures / "ORFAN0231").glob("IM-0003-0001.dcm"))
    rows = pv.header_diff(f, "CBB0999")
    by_kw = {r.keyword: r for r in rows if r.keyword and "[" not in r.path}
    assert by_kw["PatientName"].action == "changed" and by_kw["PatientName"].before == "SMITH^JOHN" and by_kw["PatientName"].after == "CBB0999"
    assert by_kw["PatientBirthDate"].action == "removed"
    assert by_kw["StudyDate"].action == "changed" and by_kw["StudyDate"].after == "19000101"
    assert by_kw["InstitutionName"].action == "changed" and by_kw["InstitutionName"].after == ""
    assert by_kw["PatientIdentityRemoved"].action == "added" and by_kw["PatientIdentityRemoved"].after == "YES"
    assert by_kw["SliceThickness"].action == "kept"
    assert any(r.keyword == "private" and r.action == "removed" for r in rows)
    assert any("[0]." in r.path and r.action == "changed" for r in rows), "dates inside sequences are diffed too"
    c = pv.diff_summary(rows)
    assert c["removed"] > 10 and c["changed"] > 10 and c["added"] >= 2 and c["kept"] > 10
    # the file on disk is untouched
    assert pydicom.dcmread(str(f), stop_before_pixels=True).PatientName == "SMITH^JOHN"


def test_load_slice_window_and_ppm(fixtures):
    f = next((fixtures / "ORFAN0231").glob("IM-0003-0001.dcm"))
    sl = pv.load_slice(f)
    assert (sl.rows, sl.cols, sl.n_frames, sl.is_rgb) == (64, 64, 1, False)
    assert sl.default_window == (200.0, 600.0)
    assert sl.array.dtype == np.float32 and sl.array.min() >= -1024, "rescale intercept applied"
    img = pv.window(sl.array, 200, 600)
    assert img.dtype == np.uint8 and img.shape == (64, 64)
    full = pv.window(sl.array, 0, 0)
    assert full.min() == 0 and full.max() == 255
    small = pv.resample(img, 300, 150)
    assert small.shape == (150, 150)
    data = pv.to_ppm(small)
    assert data.startswith(b"P5 150 150 255\n") and len(data) == len(b"P5 150 150 255\n") + 150 * 150
    assert pv.WINDOW_PRESETS["Coronary (300 / 800)"] == (300.0, 800.0)


def test_redaction_paints_box_and_release_moves_file(anonymised, fixtures):
    out = anonymised
    review = list((out / "_review" / "CBB0801").glob("*.dcm"))
    assert review, "the dose SR was quarantined"
    # SRs have no pixels; redact a CT copy placed in _review to exercise the pixel path
    ct_out = next((out / "CBB0801").rglob("*.dcm"))
    fake = out / "_review" / "CBB0801" / "sc_test.dcm"
    ds = pydicom.dcmread(str(ct_out))
    ds.save_as(str(fake))
    before = pydicom.dcmread(str(fake)).pixel_array.copy()
    dest = pv.release_from_quarantine(out, "CBB0801", fake, [pv.Box(5, 5, 20, 30), pv.Box(60, 60, 200, 200)])
    assert not fake.exists() and dest.exists() and dest.parent == out / "CBB0801" / f"S{int(ds.SeriesNumber):03d}"
    after = pydicom.dcmread(str(dest))
    arr = after.pixel_array
    assert arr[5:30, 5:20].max() == 0 and arr[60:64, 60:64].max() == 0, "boxes painted, clipped to the image"
    assert np.array_equal(arr[35:55, 35:55], before[35:55, 35:55]), "outside the boxes untouched"
    assert after.BurnedInAnnotation == "NO" and after.PatientName == "CBB0801"
    logs = list((out / "_logs").glob("redactions_*.csv"))
    assert logs and "sc_test.dcm" in logs[0].read_text()
    assert pv.latest_redaction_time(out) > 0
    # originals untouched throughout
    assert pydicom.dcmread(str(next((fixtures / "ORFAN0231").glob("IM-0003-0001.dcm")))).PatientName == "SMITH^JOHN"


def test_release_refuses_files_outside_review(anonymised):
    ct_out = next((anonymised / "CBB0801").rglob("*.dcm"))
    with pytest.raises(ValueError):
        pv.release_from_quarantine(anonymised, "CBB0801", ct_out, [pv.Box(0, 0, 1, 1)])


def test_scan_output_patient_lists_kept_and_quarantined(anonymised):
    series = pv.scan_output_patient(anonymised, "CBB0801")
    verdicts = {s.verdict for s in series}
    assert "keep" in verdicts and "review" in verdicts
    assert any(s.reason.startswith("quarantined") for s in series)


def test_series_override_roundtrip(fixtures, tmp_path):
    series = pv.scan_patient(fixtures / "ORFAN0231")
    picks = tmp_path / "series_picks.csv"
    path, warning = pv.set_series_override(picks, "CBB0801", series[1])
    assert path == picks and warning == ""
    assert load_series_picks(str(picks))["CBB0801"][0]["uid"] == series[1].uid
    assert pv.current_override(picks, "CBB0801") == series[1].uid
    # replacing keeps one row per patient; a thick pick warns
    thick = series[0]
    thick.thickness = 2.0
    _, warning = pv.set_series_override(picks, "CBB0801", thick)
    assert "2 mm" in warning and len(picks.read_text().strip().splitlines()) == 2
    pv.remove_series_override(picks, "CBB0801")
    assert pv.current_override(picks, "CBB0801") == ""
    assert pv.default_picks_path(str(tmp_path / "cohort.csv")).name == "series_picks.csv"


def test_manifest_patients(fixtures, tmp_path):
    m = tmp_path / "m.csv"
    m.write_text(f"source_folder,study_id\n{fixtures / 'ORFAN0231'},CBB0901\n{fixtures / 'ORFAN0418'},CBB0902\n")
    pats = pv.manifest_patients(str(m))
    assert [p[0] for p in pats] == ["CBB0901", "CBB0902"] and pats[0][1].is_dir()


def test_bilinear_resample_and_thumbnail(fixtures):
    f = next((fixtures / "ORFAN0231").glob("IM-0003-0001.dcm"))
    sl = pv.load_slice(f)
    img = pv.window(sl.array, 200, 600)
    up = pv.resample_to(img, 200, 200, smooth=True)
    assert up.shape == (200, 200) and up.dtype == np.uint8
    # bilinear upscaling produces intermediate values between neighbours, nearest does not
    nearest = pv.resample_to(img, 200, 200, smooth=False)
    assert len(np.unique(up)) >= len(np.unique(nearest))
    assert pv.resample(img, 100, 300, aspect=2.0).shape == (200, 100), "aspect scales rows"
    th = pv.thumbnail(f, 32)
    assert th.shape == (32, 32)
    assert pv.thumbnail(next((fixtures / "ORFAN0231").glob("SR-*.dcm")), 32) is None, "no pixels -> no thumbnail"


def test_volume_and_reformats(fixtures):
    series = pv.scan_patient(fixtures / "ORFAN0231")
    ct = series[1]  # 6 slices
    vol = pv.load_volume(ct)
    assert vol.array.shape == (6, 64, 64) and vol.array.dtype == np.float32
    dz, dy, dx = vol.spacing
    assert abs(dz - 0.5) < 1e-6 and abs(dy - 0.44) < 1e-6 and abs(dx - 0.44) < 1e-6
    assert vol.positions == sorted(vol.positions) and vol.default_window == (200.0, 600.0)
    ax, aspect, n = pv.mpr_slice(vol, "Axial", 2)
    assert ax.shape == (64, 64) and n == 6 and abs(aspect - 1.0) < 1e-6
    co, aspect, n = pv.mpr_slice(vol, "Coronal", 10)
    assert co.shape == (6, 64) and n == 64 and abs(aspect - 0.5 / 0.44) < 1e-6
    assert np.array_equal(co[-1], vol.array[0, 10, :]), "coronal is shown head-up (last row = lowest slice)"
    sa, aspect, n = pv.mpr_slice(vol, "Sagittal", 999)
    assert sa.shape == (6, 64) and n == 64, "index clamped"


def test_release_of_a_file_without_pixels(anonymised):
    sr = next((anonymised / "_review" / "CBB0801").glob("SR-*.dcm"), None) or next((anonymised / "_review" / "CBB0801").glob("*.dcm"))
    with pytest.raises(ValueError):
        pv.release_from_quarantine(anonymised, "CBB0801", sr, [pv.Box(0, 0, 5, 5)])
    assert sr.exists(), "refused: nothing moved"
    dest = pv.release_from_quarantine(anonymised, "CBB0801", sr, [])
    assert dest.exists() and not sr.exists() and pydicom.dcmread(str(dest)).Modality == "SR"   # runs last: it empties the quarantine
