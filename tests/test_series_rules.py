"""Unit tests for the coronary-series classifier used by --ctca-only."""
import pydicom
from pydicom.dataset import Dataset
from scrubdicom.core import classify_series, MAX_SLICE_MM


def hdr(**kw):
    ds = Dataset()
    ds.Modality = "CT"; ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]; ds.SliceThickness = "0.75"; ds.ReconstructionDiameter = "200"
    ds.ContrastBolusAgent = "OMNIPAQUE"; ds.SeriesDescription = "CorCTA 75%"
    for k, v in kw.items():
        setattr(ds, k, v)
    return ds


def test_keeps_thin_contrast_cardiac():
    assert classify_series(hdr(), 300)[0] == "keep"


def test_drops_thick():
    assert classify_series(hdr(SliceThickness="1.0"), 300)[0] == "drop"
    assert classify_series(hdr(SliceThickness="2.0"), 300)[0] == "drop"


def test_drops_chest_fov_and_lung_kernel_and_few_images():
    assert classify_series(hdr(ReconstructionDiameter="350"), 300)[0] == "drop"
    assert classify_series(hdr(ConvolutionKernel="B70f"), 300)[0] == "drop"
    assert classify_series(hdr(), 40)[0] == "drop"


def test_ge_snapshot_pulse_is_not_a_snapshot():
    assert classify_series(hdr(SeriesDescription="SNAPSHOT PULSE"), 224)[0] == "keep"
    assert classify_series(hdr(SeriesDescription="MPR snapshot"), 224)[0] == "drop"


def test_no_contrast_is_maybe():
    assert classify_series(hdr(ContrastBolusAgent="", SeriesDescription="Series 5"), 300)[0] == "maybe"


def test_threshold_constant():
    assert MAX_SLICE_MM == 0.8
