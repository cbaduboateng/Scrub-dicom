"""Synthetic patients for the app's demo: two made-up people whose exports look like a real cardiac CT study.

Each has a scout, a calcium score, a thin contrast coronary series, a chest X-ray and a dose report; the second
also has an ultrasound frame. The images are drawn, not random: a chest phantom with lungs, spine, a contrast-filled
heart and aorta, so the viewer has something to show. Every identifier is invented and planted in the same places
the test fixtures plant them, so the demo exercises the real tag policy. No real data is involved anywhere.

Usage: python -m scrubdicom.demo_data <out_dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from pydicom.uid import generate_uid

from scrubdicom.fixtures import ct_image, dose_sr, us_image

DEMO_PATIENTS = [
    dict(folder="SMITH_JOHN_1234567", pid="1234567", name="SMITH^JOHN", dob="19610314", sex="M", sdate="20190522"),
    dict(folder="JONES_MARY_7654321", pid="7654321", name="JONES^MARY", dob="19750101", sex="F", sdate="20210110"),
]
N = 128                      # pixels per side
CTA_SLICES = 120             # the coronary rule wants 100 or more
DX_SOP = "1.2.840.10008.5.1.4.1.1.1.1"       # Digital X-Ray Image Storage - For Presentation


def _grid():
    y, x = np.mgrid[0:N, 0:N].astype("float32")
    return (x - N / 2) / (N / 2), (y - N / 2) / (N / 2)


def chest_slice(t: float, contrast: bool, seed: int) -> np.ndarray:
    """One axial slice of a chest phantom in Hounsfield units. t runs 0..1 from the top of the heart to its base."""
    x, y = _grid()
    rng = np.random.default_rng(seed)
    hu = np.full((N, N), -1000.0, dtype="float32")
    body = (x / 0.92) ** 2 + (y / 0.70) ** 2 < 1
    hu[body] = -90                                                    # fat
    hu[(x / 0.86) ** 2 + (y / 0.64) ** 2 < 1] = 45                    # chest wall / mediastinum
    for cx in (-0.46, 0.46):                                          # lungs
        hu[((x - cx) / 0.32) ** 2 + ((y + 0.02) / 0.46) ** 2 < 1] = -820
    hu[(x / 0.11) ** 2 + ((y - 0.46) / 0.11) ** 2 < 1] = 620          # vertebral body
    hu[(x / 0.05) ** 2 + ((y - 0.46) / 0.05) ** 2 < 1] = 30           # spinal canal
    hu[(x / 0.09) ** 2 + ((y + 0.60) / 0.035) ** 2 < 1] = 480         # sternum
    blood = 380 if contrast else 45
    r = 0.24 + 0.10 * np.sin(np.pi * t)                               # the heart widens then narrows
    heart = ((x + 0.06) / (r * 1.25)) ** 2 + ((y + 0.10) / r) ** 2 < 1
    hu[heart] = 105                                                   # myocardium
    hu[((x + 0.02) / (r * 0.62)) ** 2 + ((y + 0.06) / (r * 0.50)) ** 2 < 1] = blood          # left ventricle
    hu[((x + 0.20) / (r * 0.40)) ** 2 + ((y + 0.24) / (r * 0.34)) ** 2 < 1] = blood - 60     # right ventricle
    hu[((x - 0.16) / 0.085) ** 2 + ((y - 0.26) / 0.085) ** 2 < 1] = blood + 20               # descending aorta
    if t < 0.35:
        hu[((x - 0.02) / 0.10) ** 2 + ((y + 0.20) / 0.10) ** 2 < 1] = blood + 30             # aortic root
    if contrast:                                                      # three coronary arteries drifting with depth
        for ang0, sweep, rr in ((2.3, 1.4, 1.04), (4.0, 1.1, 1.06), (5.6, -1.2, 1.03)):
            a = ang0 + sweep * t
            cx, cy = -0.06 + np.cos(a) * r * 1.25 * rr, -0.10 + np.sin(a) * r * rr
            hu[(x - cx) ** 2 + (y - cy) ** 2 < 0.018 ** 2] = 420
    hu += rng.normal(0, 14 if contrast else 22, hu.shape).astype("float32") * body
    return hu


def scout() -> np.ndarray:
    """A frontal localiser: body outline, bright spine, darker lung fields."""
    x, y = _grid()
    img = np.full((N, N), -1000.0, dtype="float32")
    img[np.abs(x) < 0.80 - 0.10 * y] = 60
    for cx in (-0.36, 0.36):
        img[((x - cx) / 0.26) ** 2 + ((y + 0.15) / 0.52) ** 2 < 1] = -520
    img[np.abs(x) < 0.07] = 520
    img[((x + 0.10) / 0.30) ** 2 + ((y - 0.18) / 0.26) ** 2 < 1] = 220
    return img


def _set_pixels(ds, hu: np.ndarray) -> None:
    ds.Rows, ds.Columns = hu.shape
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
    ds.RescaleIntercept, ds.RescaleSlope = "-1024", "1"
    ds.PixelData = np.clip(hu + 1024, 0, 4095).astype("<u2").tobytes()


def _ct(p, sno, desc, inst, study, series, frame, hu, thick, kernel, contrast, extra=None):
    ds = ct_image(p, sno, desc, inst, study, series, frame)
    _set_pixels(ds, hu)
    ds.SliceThickness = f"{thick:g}"
    ds.ConvolutionKernel = kernel
    ds.PixelSpacing = [1.4, 1.4]
    ds.ReconstructionDiameter = "180"
    ds.ImagePositionPatient = [-90.0, -90.0, -400.0 - inst * thick]
    ds.WindowCenter, ds.WindowWidth = ("300", "800") if contrast else ("40", "400")
    if not contrast:
        for kw in ("ContrastBolusAgent", "ContrastBolusStartTime"):
            if kw in ds:
                del ds[kw]
    for k, v in (extra or {}).items():
        setattr(ds, k, v)
    return ds


def chest_xray(p, study_uid):
    """A screening chest film filed with the CT: a different modality, held back by the tag policy for a look."""
    ds = ct_image(p, 9, "Chest PA", 1, study_uid, generate_uid(), generate_uid())
    for kw in ("SliceThickness", "ConvolutionKernel", "ContrastBolusAgent", "ContrastBolusStartTime", "ImagePositionPatient",
               "ImageOrientationPatient", "RescaleIntercept", "RescaleSlope", "RescaleType", "FrameOfReferenceUID"):
        if kw in ds:
            del ds[kw]
    ds.file_meta.MediaStorageSOPClassUID = DX_SOP
    ds.SOPClassUID = DX_SOP
    ds.Modality = "DX"
    ds.Manufacturer, ds.ManufacturerModelName = "CARESTREAM", "DRX-Evolution"
    ds.ImageType = ["ORIGINAL", "PRIMARY"]
    ds.StudyDescription = "XR Chest"
    img = np.clip((scout() + 1000) / 1.6, 0, 1000)
    ds.Rows, ds.Columns = img.shape
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
    ds.PixelSpacing = [2.6, 2.6]
    ds.WindowCenter, ds.WindowWidth = "500", "1000"
    ds.PixelData = img.astype("<u2").tobytes()
    return ds


def echo_frame(p, study_uid):
    """The fixture ultrasound frame with a drawn sector (speckle and two dark chambers) in place of noise."""
    ds = us_image(p, study_uid)
    x, y = _grid()
    r, ang = np.hypot(x, y + 0.9), np.arctan2(x, y + 0.9)
    sector = (np.abs(ang) < 0.62) & (r > 0.15) & (r < 1.75)
    img = np.zeros((N, N), dtype="float32")
    img[sector] = np.random.default_rng(3).gamma(2.0, 30.0, (N, N)).astype("float32")[sector]
    for cx, cy, a, b in ((-0.13, 0.05, 0.15, 0.32), (0.17, 0.10, 0.13, 0.27)):
        img[(((x - cx) / a) ** 2 + ((y - cy) / b) ** 2 < 1) & sector] *= 0.12
    ds.Rows, ds.Columns = img.shape
    ds.PixelData = np.clip(img, 0, 255).astype("uint8").tobytes()
    return ds


def main(out) -> int:
    """Write the demo patients under `out`, one folder each. Returns the number of DICOM files written."""
    out = Path(out)
    total = 0
    for pi, p in enumerate(DEMO_PATIENTS):
        d = out / p["folder"]
        study, frame = generate_uid(prefix="1.3.12.2.1107.5.1.4."), generate_uid(prefix="1.3.12.2.1107.5.1.4.")

        def save(ds, sub: str, name: str) -> None:
            nonlocal total
            (d / sub).mkdir(parents=True, exist_ok=True)
            ds.save_as(str(d / sub / name), write_like_original=False)
            total += 1

        save(_ct(p, 1, "Topogram 0.6 T20f", 1, study, generate_uid(), frame, scout(), 0.6, "T20f", False,
                 {"ImageType": ["ORIGINAL", "PRIMARY", "LOCALIZER"]}), "S001_Topogram", "IM0001.dcm")
        suid = generate_uid()
        for i in range(1, 17):
            save(_ct(p, 2, "CaScore 3.0 Qr36", i, study, suid, frame, chest_slice(i / 16, False, 100 * pi + i), 3.0, "Qr36f", False),
                 "S002_CaScore", f"IM{i:04d}.dcm")
        suid = generate_uid()
        for i in range(1, CTA_SLICES + 1):
            save(_ct(p, 5, "CorCTA 0.6 Bv40 BestDiast 75 %", i, study, suid, frame, chest_slice(i / CTA_SLICES, True, 1000 * (pi + 1) + i), 0.6, "Bv40f", True,
                     {"NominalPercentageOfCardiacPhase": 75.0}), "S005_CorCTA", f"IM{i:04d}.dcm")
        save(chest_xray(p, study), "S009_ChestXR", "IM0001.dcm")
        save(dose_sr(p, study), "S501_Dose", "SR0001.dcm")
        if pi == 1:
            save(echo_frame(p, study), "S007_Echo", "IM0001.dcm")
        (d / "README.txt").write_text("synthetic demo patient, not a real person\n")
    return total


if __name__ == "__main__":
    n = main(sys.argv[1] if len(sys.argv) > 1 else "demo_patients")
    print(f"{n} DICOM files written")
