"""Logic behind the in-app viewer: no Tk here, everything unit-testable.

- scan_patient():      one patient folder -> list of Series (files, representative header, keep/drop verdict)
- header_diff():       what the engine would do to one file, computed in memory: before / after / action per tag
- load_slice():        pixel data of one file as a float array in Hounsfield units (or raw values)
- window():            Hounsfield array -> 8-bit grey with a window centre / width
- to_pgm():            8-bit array -> PGM bytes that tkinter.PhotoImage can display without Pillow
- apply_redaction():   paint rectangles black in the anonymised copy of a quarantined file and release it
- set_series_override(): record "keep this series for this patient" in a series-pick CSV the engine reads

The original scans are never written to. Redaction only ever touches files under the output tree.
"""
from __future__ import annotations

import copy
import csv
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset

from scrubdicom.core import (MAX_SLICE_MM, SERIES_TAGS, anonymise_dataset, classify_series, is_review_object,
                             iter_dicom_files, load_series_picks, series_info, safe_name)

try:
    import numpy as np
except ImportError:  # the viewer needs numpy; the rest of the app does not
    np = None

PREVIEW_SALT = "preview-only-salt"
HEADER_TAGS = SERIES_TAGS + ["InstanceNumber", "ImagePositionPatient", "Rows", "Columns", "BurnedInAnnotation",
                             "WindowCenter", "WindowWidth", "StudyInstanceUID", "PatientID", "PatientName",
                             "KVP", "XRayTubeCurrent", "Exposure", "CTDIvol", "PixelSpacing"]

WINDOW_PRESETS: dict[str, tuple[float, float] | None] = {
    "From header": None,
    "Coronary (300 / 800)": (300.0, 800.0),
    "Soft tissue (40 / 400)": (40.0, 400.0),
    "Lung (-600 / 1500)": (-600.0, 1500.0),
    "Bone (300 / 1500)": (300.0, 1500.0),
    "Full range": (0.0, 0.0),   # special: min..max of the slice
}


def viewer_available() -> tuple[bool, str]:
    if np is None:
        return False, "numpy is not installed (pip install \"scrub-dicom[viewer]\")"
    return True, ""


# ---------------------------------------------------------------------------------------------- series scan

@dataclass
class Series:
    uid: str
    number: str
    description: str
    modality: str
    thickness: float | None
    kernel: str
    fov: float | None
    frames: int
    files: list[Path]
    header: Dataset
    verdict: str          # keep | maybe | drop
    reason: str
    review: str | None    # quarantine reason, if any
    kvp: str = ""
    mas: str = ""
    ctdi: str = ""

    @property
    def n_images(self) -> int:
        return max(len(self.files), self.frames if len(self.files) == 1 else 0)

    @property
    def label(self) -> str:
        return f"S{self.number}: {self.description or '(no description)'}"


def _num_str(v) -> str:
    """'120.0' -> '120', None -> ''. Header numbers arrive as DS strings or floats."""
    if v is None or v == "":
        return ""
    try:
        f = float(v)
        return f"{f:g}"
    except (TypeError, ValueError):
        return str(v)


def _sort_key(ds: Dataset):
    try:
        return (0, float(ds.get("InstanceNumber", 0) or 0))
    except (TypeError, ValueError):
        return (1, 0.0)


def scan_patient(folder: Path, progress=None) -> list[Series]:
    """Group every DICOM file under `folder` by series and classify each series exactly as --ctca-only does.
    `progress(n_files)` is called every 200 files so a window can show life."""
    groups: dict[str, list[tuple[Path, Dataset]]] = {}
    n = 0
    for path in iter_dicom_files(Path(folder), None):
        try:
            h = pydicom.dcmread(str(path), stop_before_pixels=True, specific_tags=HEADER_TAGS, force=True)
        except Exception:
            continue
        if "SOPClassUID" not in h:
            continue
        uid = str(h.get("SeriesInstanceUID", "")) or f"nouid-{h.get('SeriesNumber', 0)}"
        groups.setdefault(uid, []).append((path, h))
        n += 1
        if progress and n % 200 == 0:
            progress(n)
    out: list[Series] = []
    for uid, items in groups.items():
        items.sort(key=lambda t: _sort_key(t[1]))
        head = items[0][1]
        thick, kern, fov, _ = series_info(head)
        verdict, reason = classify_series(head, len(items))
        out.append(Series(uid=uid, number=str(head.get("SeriesNumber", "") or ""), description=str(head.get("SeriesDescription", "") or ""),
                          modality=str(head.get("Modality", "") or ""), thickness=thick, kernel=kern, fov=fov,
                          frames=int(head.get("NumberOfFrames", 0) or 0), files=[p for p, _ in items], header=head,
                          verdict=verdict, reason=reason, review=is_review_object(head),
                          kvp=_num_str(head.get("KVP")), mas=_num_str(head.get("Exposure") or head.get("XRayTubeCurrent")), ctdi=_num_str(head.get("CTDIvol"))))
    out.sort(key=lambda s: (float(s.number) if s.number.replace(".", "").isdigit() else 1e9, s.description))
    return out


def scan_output_patient(out: Path, study_id: str) -> list[Series]:
    """Series of an already anonymised patient: the S### folders plus the quarantined files in _review."""
    result: list[Series] = []
    study = out / safe_name(study_id)
    review = out / "_review" / safe_name(study_id)
    for root in ([study] if study.is_dir() else []) + ([review] if review.is_dir() else []):
        for s in scan_patient(root):
            if root == review:
                s.reason = "quarantined: " + (s.review or "needs a human look")
                s.verdict = "review"
            else:
                s.verdict, s.reason = "keep", "in the anonymised output"
            result.append(s)
    return result


# ---------------------------------------------------------------------------------------------- header diff

@dataclass
class DiffRow:
    path: str        # "(0010,0010)" or "(0040,0275)[0].(0040,1001)" inside a sequence
    keyword: str
    vr: str
    before: str
    after: str
    action: str      # removed | changed | added | kept


def _val(elem) -> str:
    if elem.VR == "SQ":
        return f"<sequence of {len(elem.value)}>"
    if elem.VR in ("OB", "OW", "OF", "UN", "OD", "OL", "OV"):
        return f"<{len(elem.value) if elem.value is not None else 0} bytes>"
    v = elem.value
    try:
        s = "\\".join(str(x) for x in v) if elem.VM > 1 else str(v)
    except Exception:
        s = repr(v)
    return s if len(s) <= 80 else s[:77] + "..."


def _flatten(ds: Dataset, prefix: str = "") -> dict[str, tuple[str, str, str]]:
    out: dict[str, tuple[str, str, str]] = {}
    for elem in ds:
        key = f"{prefix}{elem.tag}"
        kw = elem.keyword or ("private" if elem.tag.is_private else "")
        out[key] = (kw, elem.VR, _val(elem))
        if elem.VR == "SQ":
            for i, item in enumerate(elem.value):
                out.update(_flatten(item, f"{key}[{i}]."))
    return out


def header_diff(path: Path, study_id: str, salt: str = PREVIEW_SALT, keep_technical: bool = False, profile=None) -> list[DiffRow]:
    """Read one file's header, run the real anonymise_dataset on an in-memory copy, and list every tag with what
    happened to it. The file on disk is not touched."""
    ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    before = _flatten(ds)
    anon = copy.deepcopy(ds)
    anonymise_dataset(anon, study_id, salt, keep_technical, profile)
    after = _flatten(anon)
    rows: list[DiffRow] = []
    for key, (kw, vr, val) in before.items():
        if key not in after:
            rows.append(DiffRow(key, kw, vr, val, "", "removed"))
        elif after[key][2] != val:
            rows.append(DiffRow(key, kw, vr, val, after[key][2], "changed"))
        else:
            rows.append(DiffRow(key, kw, vr, val, val, "kept"))
    for key, (kw, vr, val) in after.items():
        if key not in before:
            rows.append(DiffRow(key, kw, vr, "", val, "added"))
    order = {"removed": 0, "changed": 1, "added": 2, "kept": 3}
    rows.sort(key=lambda r: (order[r.action], r.path))
    return rows


def header_rows(path: Path) -> list[DiffRow]:
    """The header of an already anonymised file, in the same row shape (everything 'kept')."""
    ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    return [DiffRow(k, kw, vr, val, val, "kept") for k, (kw, vr, val) in _flatten(ds).items()]


def diff_summary(rows: list[DiffRow]) -> dict[str, int]:
    c = {"removed": 0, "changed": 0, "added": 0, "kept": 0}
    for r in rows:
        c[r.action] += 1
    return c


# ---------------------------------------------------------------------------------------------- pixels

@dataclass
class Slice:
    array: "np.ndarray"          # 2-D float32 (Hounsfield if rescale present) or 3-D uint8 RGB
    rows: int
    cols: int
    n_frames: int
    default_window: tuple[float, float] | None
    is_rgb: bool = False
    note: str = ""


def load_slice(path: Path, frame: int = 0) -> Slice:
    if np is None:
        raise RuntimeError("numpy is not installed")
    ds = pydicom.dcmread(str(path), force=True)
    arr = ds.pixel_array          # raises if the transfer syntax has no decoder installed
    n_frames = int(ds.get("NumberOfFrames", 1) or 1)
    if arr.ndim == 4 or (arr.ndim == 3 and n_frames > 1 and arr.shape[0] == n_frames):
        arr = arr[min(frame, arr.shape[0] - 1)]
    is_rgb = arr.ndim == 3 and arr.shape[-1] == 3
    default = None
    if is_rgb:
        arr = arr.astype(np.uint8)
    else:
        arr = arr.astype(np.float32)
        slope = float(ds.get("RescaleSlope", 1) or 1)
        inter = float(ds.get("RescaleIntercept", 0) or 0)
        arr = arr * slope + inter
        wc, ww = ds.get("WindowCenter"), ds.get("WindowWidth")
        try:
            if wc is not None and ww is not None:
                wc = float(wc[0] if getattr(wc, "__len__", None) and not isinstance(wc, str) else wc)
                ww = float(ww[0] if getattr(ww, "__len__", None) and not isinstance(ww, str) else ww)
                if ww > 0:
                    default = (wc, ww)
        except (TypeError, ValueError, IndexError):
            default = None
    return Slice(arr, int(arr.shape[0]), int(arr.shape[1]), n_frames, default, is_rgb)


def window(arr: "np.ndarray", centre: float, width: float) -> "np.ndarray":
    """Linear window/level to uint8. width <= 0 means 'full range of the slice'."""
    if arr.ndim == 3:
        return arr
    if width <= 0:
        lo, hi = float(arr.min()), float(arr.max())
        if hi <= lo:
            hi = lo + 1
    else:
        lo, hi = centre - width / 2.0, centre + width / 2.0
    out = (arr - lo) * (255.0 / (hi - lo))
    return np.clip(out, 0, 255).astype(np.uint8)


def resample_to(img8: "np.ndarray", nw: int, nh: int, smooth: bool = True) -> "np.ndarray":
    """Resize to exactly nw x nh. Bilinear when smooth (what a viewer does), nearest otherwise (fast, for thumbnails)."""
    h, w = img8.shape[:2]
    nw, nh = max(1, int(nw)), max(1, int(nh))
    if not smooth or (nw <= w and nh <= h and (w // nw > 2 or h // nh > 2)):
        ys = np.linspace(0, h - 1, nh).round().astype(int)
        xs = np.linspace(0, w - 1, nw).round().astype(int)
        return img8[ys][:, xs]
    ys = np.linspace(0, h - 1, nh)
    xs = np.linspace(0, w - 1, nw)
    y0 = np.floor(ys).astype(int)
    x0 = np.floor(xs).astype(int)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    wy = (ys - y0).astype(np.float32)
    wx = (xs - x0).astype(np.float32)
    src = img8.astype(np.float32)
    if src.ndim == 3:
        wy, wx = wy[:, None, None], wx[None, :, None]
    else:
        wy, wx = wy[:, None], wx[None, :]
    top = src[y0][:, x0] * (1 - wx) + src[y0][:, x1] * wx
    bot = src[y1][:, x0] * (1 - wx) + src[y1][:, x1] * wx
    return (top * (1 - wy) + bot * wy).round().clip(0, 255).astype(np.uint8)


def resample(img8: "np.ndarray", target_w: int, target_h: int, aspect: float = 1.0, smooth: bool = True) -> "np.ndarray":
    """Resize preserving aspect ratio (rows scaled by `aspect`, the row-spacing / column-spacing ratio) so the
    result fits inside target_w x target_h."""
    h, w = img8.shape[:2]
    scale = min(target_w / w, target_h / (h * aspect))
    return resample_to(img8, w * scale, h * scale * aspect, smooth)


def thumbnail(path: Path, size: int = 64) -> "np.ndarray | None":
    """Small grey square for the series list; None when the file has no pixels or cannot be decoded."""
    try:
        sl = load_slice(path)
    except Exception:
        return None
    wc, ww = sl.default_window or (40.0, 400.0)
    img = window(sl.array, wc, ww)
    if img.ndim == 3:
        img = img.mean(axis=2).astype(np.uint8)
    return resample(img, size, size, 1.0, smooth=False)


# ---------------------------------------------------------------------------------------------- volume / MPR

@dataclass
class Volume:
    array: "np.ndarray"                 # [z, y, x] float32 Hounsfield, z increasing towards the head
    spacing: tuple[float, float, float]  # (dz, dy, dx) in mm
    positions: list[float]              # z of each slice, ascending
    default_window: tuple[float, float] | None


def load_volume(series: Series, progress=None) -> Volume:
    """Stack every slice of a series into one array, ordered by position along the slice axis, so coronal and
    sagittal reformats can be cut from it. Needs numpy; a 300-slice 512x512 CTCA is ~300 MB as float32."""
    if np is None:
        raise RuntimeError("numpy is not installed")
    slices: list[tuple[float, "np.ndarray"]] = []
    default, dy, dx = None, 1.0, 1.0
    for i, path in enumerate(series.files):
        ds = pydicom.dcmread(str(path), force=True)
        arr = ds.pixel_array
        if arr.ndim != 2:
            raise ValueError("Reformats need a stack of single-frame greyscale images")
        arr = arr.astype(np.float32) * float(ds.get("RescaleSlope", 1) or 1) + float(ds.get("RescaleIntercept", 0) or 0)
        ipp = ds.get("ImagePositionPatient")
        z = float(ipp[2]) if ipp is not None and len(ipp) == 3 else float(i)
        slices.append((z, arr))
        if i == 0:
            ps = ds.get("PixelSpacing")
            if ps is not None and len(ps) == 2:
                dy, dx = float(ps[0]), float(ps[1])
            wc, ww = ds.get("WindowCenter"), ds.get("WindowWidth")
            try:
                if wc is not None and ww is not None:
                    wc = float(wc[0] if getattr(wc, "__len__", None) and not isinstance(wc, str) else wc)
                    ww = float(ww[0] if getattr(ww, "__len__", None) and not isinstance(ww, str) else ww)
                    default = (wc, ww) if ww > 0 else None
            except (TypeError, ValueError, IndexError):
                default = None
        if progress and i % 25 == 0:
            progress(i + 1, len(series.files))
    slices.sort(key=lambda t: t[0])
    zs = [z for z, _ in slices]
    dz = float(np.median(np.diff(zs))) if len(zs) > 1 else 1.0
    if dz <= 0:
        dz = 1.0
    vol = np.stack([a for _, a in slices])
    return Volume(vol, (dz, dy, dx), zs, default)


ORIENTATIONS = ("Axial", "Coronal", "Sagittal")


def mpr_slice(vol: Volume, orientation: str, index: int) -> tuple["np.ndarray", float, int]:
    """One reformatted slice, its row/column spacing ratio (for display aspect), and the number of slices in that
    orientation. Coronal and sagittal are shown head-up."""
    dz, dy, dx = vol.spacing
    nz, ny, nx = vol.array.shape
    if orientation == "Coronal":
        i = max(0, min(ny - 1, index))
        return vol.array[::-1, i, :], dz / dx, ny
    if orientation == "Sagittal":
        i = max(0, min(nx - 1, index))
        return vol.array[::-1, :, i], dz / dy, nx
    i = max(0, min(nz - 1, index))
    return vol.array[i], dy / dx, nz


def to_ppm(img8: "np.ndarray") -> bytes:
    """PGM (grey) or PPM (RGB) bytes for tkinter.PhotoImage(data=...)."""
    h, w = img8.shape[:2]
    if img8.ndim == 3:
        return b"P6 %d %d 255\n" % (w, h) + np.ascontiguousarray(img8).tobytes()
    return b"P5 %d %d 255\n" % (w, h) + np.ascontiguousarray(img8).tobytes()


# ---------------------------------------------------------------------------------------------- redaction

@dataclass
class Box:
    x0: int
    y0: int
    x1: int
    y1: int

    def norm(self, rows: int, cols: int) -> "Box":
        x0, x1 = sorted((max(0, min(self.x0, cols)), max(0, min(self.x1, cols))))
        y0, y1 = sorted((max(0, min(self.y0, rows)), max(0, min(self.y1, rows))))
        return Box(x0, y0, x1, y1)


def apply_redaction(src: Path, boxes: list[Box], dest: Path | None = None) -> Path:
    """Paint the boxes with the darkest value in the anonymised file and write it (in place, or to dest).
    Compressed pixel data is decoded and written uncompressed. Only ever called on files under the output tree."""
    if np is None:
        raise RuntimeError("numpy is not installed")
    ds = pydicom.dcmread(str(src), force=True)
    target = Path(dest) if dest else Path(src)
    if "PixelData" not in ds:
        # structured reports, PDFs: nothing to paint. Boxes make no sense; an unchanged release is a copy.
        if boxes:
            raise ValueError("This file has no pixel data; there is nothing to redact")
        if target != Path(src):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        return target
    arr = ds.pixel_array
    fill = 0 if ds.get("PhotometricInterpretation", "") != "MONOCHROME1" else int(arr.max())
    frames = arr if arr.ndim == 4 or (arr.ndim == 3 and int(ds.get("NumberOfFrames", 1) or 1) > 1 and arr.shape[-1] != 3) else [arr]
    rows, cols = int(ds.Rows), int(ds.Columns)
    for f in frames:
        for b in boxes:
            b = b.norm(rows, cols)
            if b.x1 > b.x0 and b.y1 > b.y0:
                f[b.y0:b.y1, b.x0:b.x1] = fill
    arr = np.asarray(frames if len(frames) > 1 else frames[0]).astype(arr.dtype)
    from pydicom.uid import ExplicitVRLittleEndian
    if ds.file_meta.TransferSyntaxUID.is_compressed:
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.is_little_endian, ds.is_implicit_VR = True, False
        if "PlanarConfiguration" in ds and arr.ndim == 3:
            ds.PlanarConfiguration = 0
    ds.PixelData = arr.tobytes()
    ds.BurnedInAnnotation = "NO"
    target.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(target), write_like_original=False)
    return target


def release_from_quarantine(out: Path, study_id: str, src: Path, boxes: list[Box], who: str = "viewer") -> Path:
    """Redact a file that sits in <out>/_review/<study>/ and move it into the study's output folder, logging the
    action to _logs/redactions_<date>.csv so the share checklist knows verify must run again."""
    out = Path(out)
    review_dir = out / "_review" / safe_name(study_id)
    src = Path(src)
    if review_dir.resolve() not in src.resolve().parents:
        raise ValueError("Only files inside the study's _review folder can be released")
    ds = pydicom.dcmread(str(src), stop_before_pixels=True, force=True)
    try:
        sno = int(float(str(ds.get("SeriesNumber", "0") or "0")))
    except ValueError:
        sno = 0
    dest = out / safe_name(study_id) / f"S{sno:03d}" / src.name
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}_released{dest.suffix}")
    apply_redaction(src, boxes, dest)
    os.remove(src)
    log = out / "_logs" / f"redactions_{time.strftime('%Y%m%d')}.csv"
    log.parent.mkdir(parents=True, exist_ok=True)
    new = not log.exists()
    with open(log, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["time", "study_id", "file", "released_to", "boxes", "by"])
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), study_id, src.name, str(dest.relative_to(out)),
                    ";".join(f"{b.x0},{b.y0},{b.x1},{b.y1}" for b in boxes), who])
    return dest


def latest_redaction_time(out: Path) -> float:
    best = 0.0
    try:
        for p in (Path(out) / "_logs").glob("redactions_*.csv"):
            best = max(best, p.stat().st_mtime)
    except OSError:
        pass
    return best


# ---------------------------------------------------------------------------------------------- overrides

def default_picks_path(manifest: str) -> Path:
    """Where the app records 'keep this series instead' decisions: next to the patient list, so it stays with the
    confidential inputs and the engine's --series-pick can read it."""
    return Path(manifest).resolve().with_name("series_picks.csv")


def set_series_override(picks_path: Path, study_id: str, series: Series) -> tuple[Path, str]:
    """Add or replace the row for study_id in a series-pick CSV. Returns (path, warning-or-empty)."""
    picks_path = Path(picks_path)
    rows: list[dict] = []
    if picks_path.exists():
        with open(picks_path, newline="", encoding="utf-8-sig") as fh:
            rows = [r for r in csv.DictReader(fh) if (r.get("study_id") or "").strip() != study_id]
    rows.append({"study_id": study_id, "series_uid": series.uid if not series.uid.startswith("nouid-") else "",
                 "series_description": series.description})
    with open(picks_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["study_id", "series_uid", "series_description"])
        w.writeheader()
        w.writerows(rows)
    warning = ""
    if series.thickness is not None and series.thickness > MAX_SLICE_MM:
        warning = (f"This series is {series.thickness:g} mm. The engine keeps series of {MAX_SLICE_MM:g} mm or thinner, so it will "
                   f"fall back to the thin coronary recon and flag the patient 'needs a look'.")
    return picks_path, warning


def current_override(picks_path: Path | None, study_id: str) -> str:
    if not picks_path or not Path(picks_path).exists():
        return ""
    for pk in load_series_picks(str(picks_path)).get(study_id, []):
        return pk["uid"] or pk["desc"]
    return ""


def remove_series_override(picks_path: Path, study_id: str) -> None:
    picks_path = Path(picks_path)
    if not picks_path.exists():
        return
    with open(picks_path, newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("study_id") or "").strip() != study_id]
    with open(picks_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["study_id", "series_uid", "series_description"])
        w.writeheader()
        w.writerows(rows)


def manifest_patients(manifest: str, remap: str | None = None) -> list[tuple[str, Path]]:
    """(study_id, source folder) pairs from the patient list, using the engine's own loader so remap applies."""
    from scrubdicom.core import load_manifest, find_folder
    try:
        return [(sid, find_folder(folder)) for folder, sid in load_manifest(manifest, remap or None)]
    except SystemExit:
        return []

