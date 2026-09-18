"""Optional, offline burned-in-text detection for quarantined objects (secondary captures, ultrasound, angiography).

Uses the Tesseract command-line program if it is installed on the computer (`tesseract` on PATH); nothing is
bundled, nothing is sent anywhere. Without Tesseract every function returns "" and the caller behaves as before.
The image is written as an 8-bit PGM to a temporary file, which Tesseract reads directly.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def available() -> bool:
    return shutil.which("tesseract") is not None


def _to_pgm(arr) -> bytes:
    import numpy as np
    a = np.asarray(arr)
    if a.ndim == 3:
        a = a[..., :3].mean(axis=2)
    a = a.astype("float32")
    lo, hi = float(a.min()), float(a.max())
    if hi <= lo:
        hi = lo + 1
    g = ((a - lo) * (255.0 / (hi - lo))).round().clip(0, 255).astype("uint8")
    # Tesseract wants some size to work with; upscale tiny images by nearest neighbour
    h, w = g.shape
    scale = max(1, int(600 / max(h, w)))
    if scale > 1:
        g = np.repeat(np.repeat(g, scale, axis=0), scale, axis=1)
        h, w = g.shape
    return b"P5 %d %d 255\n" % (w, h) + g.tobytes()


def words_in_array(arr, min_chars: int = 3) -> str:
    """Alphanumeric words Tesseract can read in the image, joined by spaces; "" if none or unavailable."""
    if not available():
        return ""
    try:
        data = _to_pgm(arr)
    except Exception:
        return ""
    fd, path = tempfile.mkstemp(suffix=".pgm")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        r = subprocess.run(["tesseract", path, "stdout", "--psm", "11"], capture_output=True, text=True, timeout=60)
        words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'^.-]*", r.stdout) if len(w) >= min_chars]
        return " ".join(words)[:200]
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def words_in_dataset(ds) -> str:
    """OCR of a pydicom dataset's first frame; "" when there are no pixels, no numpy, or no Tesseract."""
    if not available() or "PixelData" not in ds:
        return ""
    try:
        arr = ds.pixel_array
        if arr.ndim == 4 or (arr.ndim == 3 and arr.shape[-1] != 3):
            arr = arr[0]
        return words_in_array(arr)
    except Exception:
        return ""


def words_in_file(path: Path) -> str:
    try:
        import pydicom
        return words_in_dataset(pydicom.dcmread(str(path), force=True))
    except Exception:
        return ""
