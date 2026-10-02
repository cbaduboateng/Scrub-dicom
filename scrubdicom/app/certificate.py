"""A one-page PDF certificate for a verified output, written with nothing but the standard library. No Tk.

The engine's verify step already writes a machine-readable attestation (JSON) and a checksum manifest on PASS. This
turns the newest attestation into a page a person can read, file and countersign: what was anonymised, what was
removed, what was kept, that every file was re-read, and what the check does not cover. It carries new IDs and
counts only, never an original identifier, so it may travel with the output. The page is drawn directly as PDF
operators with the standard Helvetica fonts: no dependency, no font files, a few kilobytes.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from . import model
from .plain import KEPT_WORDS

W, H = 595.0, 842.0          # A4 in points
MARGIN = 50.0
NAVY, TEAL, GREEN, GREY, LIGHT, INK, WHITE = ((0.047, 0.149, 0.259), (0.0, 0.729, 0.659), (0.0, 0.561, 0.510), (0.37, 0.42, 0.49),
                                              (0.925, 0.949, 0.969), (0.07, 0.09, 0.15), (1.0, 1.0, 1.0))
# Helvetica advance widths (1/1000 em) for the printable ASCII range; other characters are taken as 556
_HELV = dict(zip(range(32, 127), (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278,
    584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944,
    667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
    278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584)))
MAX_ID_LINES = 6
# the engine's attestation says the same thing in its own vocabulary; this is the wording for a person
STATEMENT = ("Every file in the checksum list was opened again after it was written and searched. None contained hidden vendor tags, "
             "original UIDs, real dates, people's names, the identity of the hospital or the scanner (beyond anything listed under "
             "'Kept in the output'), or any of the extra words supplied.")


def text_width(s: str, size: float, bold: bool = False) -> float:
    w = sum(_HELV.get(ord(c), 556) for c in s) * size / 1000.0
    return w * 1.07 if bold else w


def wrap(s: str, size: float, width: float, bold: bool = False) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        while text_width(word, size, bold) > width and len(word) > 8:      # a long hash or ID: break it
            cut = max(8, int(len(word) * width / text_width(word, size, bold)) - 1)
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:cut])
            word = word[cut:]
        trial = (cur + " " + word).strip()
        if cur and text_width(trial, size, bold) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def id_list_lines(ids: list[str], size: float, width: float, max_lines: int = MAX_ID_LINES) -> list[str]:
    """The new IDs as comma-separated lines that fit; a long cohort is cut after max_lines with 'and N more'."""
    if not ids:
        return ["(none listed)"]
    lines, cur, used = [], "", 0
    for k, sid in enumerate(ids):
        piece = sid + ("," if k < len(ids) - 1 else "")
        trial = (cur + " " + piece).strip()
        if cur and text_width(trial, size) > width:
            if len(lines) == max_lines - 1:
                break
            lines.append(cur)
            cur = piece
        else:
            cur = trial
        used = k + 1
    lines.append(cur)
    if used < len(ids):
        lines.append(f"and {len(ids) - used} more")
    return lines


def _esc(s: str) -> str:
    s = s.encode("cp1252", "replace").decode("latin-1")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class _Page:
    def __init__(self) -> None:
        self.ops: list[str] = []

    def text(self, x: float, y: float, s: str, size: float = 10, bold: bool = False, rgb=INK, mono: bool = False) -> None:
        font = "F3" if mono else ("F2" if bold else "F1")
        self.ops.append(f"BT /{font} {size:g} Tf {rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} rg {x:.1f} {y:.1f} Td ({_esc(s)}) Tj ET")

    def right(self, x: float, y: float, s: str, size: float = 10, bold: bool = False, rgb=INK) -> None:
        self.text(x - text_width(s, size, bold), y, s, size, bold, rgb)

    def rect(self, x: float, y: float, w: float, h: float, rgb) -> None:
        self.ops.append(f"{rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} rg {x:.1f} {y:.1f} {w:.1f} {h:.1f} re f")

    def line(self, pts: list[tuple[float, float]], rgb, width: float = 0.6) -> None:
        path = " ".join(f"{x:.1f} {y:.1f} {'m' if i == 0 else 'l'}" for i, (x, y) in enumerate(pts))
        self.ops.append(f"{rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} RG {width:g} w 1 J 1 j {path} S")

    def paragraph(self, x: float, y: float, s: str, size: float, width: float, rgb=INK, leading: float | None = None) -> float:
        for ln in wrap(s, size, width):
            self.text(x, y, ln, size, rgb=rgb)
            y -= leading or size * 1.38
        return y


def _when(iso: str) -> str:
    try:
        d = _dt.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return iso
    return f"{d.day} {d.strftime('%B %Y')} at {d.strftime('%H:%M')}"


def _pdf(content: str, title: str, created: str) -> bytes:
    stream = content.encode("latin-1")
    font = lambda name: f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} /Encoding /WinAnsiEncoding >>".encode()
    stamp = "".join(c for c in created if c.isdigit())[:14].ljust(14, "0")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W:g} {H:g}] /Contents 4 0 R "
        f"/Resources << /Font << /F1 5 0 R /F2 6 0 R /F3 7 0 R >> >> >>".encode(),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        font("Helvetica"), font("Helvetica-Bold"), font("Courier"),
        f"<< /Title ({_esc(title)}) /Producer (Scrub-DICOM) /Creator (Scrub-DICOM) /CreationDate (D:{stamp}) >>".encode("latin-1"),
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, len(objs), xref)
    return bytes(out)


def certificate_pdf(att: dict, study_ids: list[str], held_back: int = 0, attestation_name: str = "") -> bytes:
    """The certificate for one attestation (the dict the engine wrote on PASS)."""
    p = _Page()
    version = str(att.get("version", ""))
    verified = str(att.get("verified_at", ""))
    out_ = att.get("output", {}) or {}
    prof = att.get("profile", {}) or {}
    chk = att.get("checksums", {}) or {}
    kept = [KEPT_WORDS.get(k, k) for k in (prof.get("retains") or [])]
    right = W - MARGIN

    # header band
    p.rect(0, H - 92, W, 92, NAVY)
    p.rect(0, H - 95, W, 3, TEAL)
    p.text(MARGIN, H - 56, "Scrub", 24, bold=True, rgb=WHITE)
    p.text(MARGIN + text_width("Scrub", 24, True) + 7, H - 56, "DICOM", 24, bold=True, rgb=TEAL)
    p.right(right, H - 48, "De-identification certificate", 13, rgb=WHITE)
    p.right(right, H - 66, "a record of an automated check", 9, rgb=(0.75, 0.82, 0.88))

    # verdict
    y = H - 150
    p.rect(MARGIN, y - 8, 30, 30, GREEN)
    p.line([(MARGIN + 7, y + 7), (MARGIN + 13, y + 1), (MARGIN + 23, y + 14)], WHITE, 3.2)
    p.text(MARGIN + 44, y + 6, "Verified: no identifiers found", 19, bold=True)
    p.text(MARGIN + 44, y - 10, f"Checked on {_when(verified)} by Scrub-DICOM {version}".strip(), 10, rgb=GREY)

    # facts
    n = int(out_.get("patients", len(study_ids)) or 0)
    done = int(out_.get("patients_marked_complete", n) or 0)
    files = int(out_.get("files_verified", 0) or 0)
    rows = [
        ("Patients anonymised", f"{n}" + ("" if done == n else f"  ({done} complete; the others are unfinished and must not be shared)")),
        ("Files checked", f"{files:,}, every one re-read after it was written"),
        ("Removed", "Everything that identifies the patient, the hospital or the scanner"),
        ("Kept in the output", "Nothing" if not kept else ", ".join(kept).capitalize()),
        ("Extra words searched for", f"{int((att.get('verify') or {}).get('needles_used', 0) or 0)} (the words themselves are not recorded)"),
        ("Files held back for a look", "None" if not held_back else f"{held_back}, in _review; not part of the verified output until released"),
        ("Result", str((att.get("verify") or {}).get("result", ""))),
    ]
    y -= 52
    label_x, value_x, value_w = MARGIN, MARGIN + 160, right - (MARGIN + 160)
    for label, value in rows:
        p.text(label_x, y, label, 9.5, rgb=GREY)
        lines = wrap(value, 10.5, value_w)
        for i, ln in enumerate(lines):
            p.text(value_x, y - i * 14, ln, 10.5, bold=(label == "Result"), rgb=(GREEN if label == "Result" else INK))
        y -= 14 * len(lines) + 5
        p.line([(MARGIN, y + 6), (right, y + 6)], LIGHT, 0.8)
        y -= 7

    # new IDs
    p.text(label_x, y, "New IDs in the output", 9.5, rgb=GREY)
    id_lines = id_list_lines(study_ids, 9, value_w)
    for i, ln in enumerate(id_lines):
        p.text(value_x, y - i * 12, ln, 9)
    y -= 12 * len(id_lines) + 6
    p.line([(MARGIN, y + 6), (right, y + 6)], LIGHT, 0.8)
    y -= 7

    # checksum list
    p.text(label_x, y, "Checksum list", 9.5, rgb=GREY)
    p.text(value_x, y, str(chk.get("file") or "(not written)"), 10.5)
    if chk.get("sha256"):
        p.text(value_x, y - 13, "SHA-256 " + str(chk["sha256"])[:32], 8, rgb=GREY, mono=True)
        p.text(value_x, y - 23, "        " + str(chk["sha256"])[32:], 8, rgb=GREY, mono=True)
        y -= 23
    y -= 26

    # statement
    lines = wrap(STATEMENT, 9.5, right - MARGIN - 28)
    box_h = len(lines) * 13 + 22
    p.rect(MARGIN, y - box_h + 14, right - MARGIN, box_h, LIGHT)
    yy = y
    for ln in lines:
        p.text(MARGIN + 14, yy, ln, 9.5)
        yy -= 13
    y -= box_h + 8

    # limits
    p.text(MARGIN, y, "What this certificate does not cover", 10.5, bold=True)
    y -= 16
    for item in ("The pictures themselves were not inspected. Text burned into an image (common in ultrasound, angiography, X-ray and "
                 "screenshots) is caught only where such files were held back for a look.",
                 "Files added to, changed in or removed from the output after the check. The checksum list detects this: re-check before handing over.",
                 "It records an automated check by a research tool. It is not a legal guarantee and Scrub-DICOM is not a medical device."):
        p.text(MARGIN + 2, y, "\u2022", 9.5, rgb=GREY)
        y = p.paragraph(MARGIN + 14, y, item, 9.5, right - MARGIN - 14, leading=12.5) - 3

    # sign-off and footer
    y = max(y - 18, 96)
    for i, label in enumerate(("Checked by", "Role", "Date")):
        x = MARGIN + i * 168
        p.line([(x, y), (x + 150, y)], GREY, 0.6)
        p.text(x, y - 12, label, 8.5, rgb=GREY)
    p.line([(MARGIN, 58), (right, 58)], LIGHT, 0.8)
    p.text(MARGIN, 44, "Contains no patient identifiers; may travel with the output.", 8.5, rgb=GREY)
    foot = f"Scrub-DICOM {version}" + (f"  \xb7  {attestation_name}" if attestation_name else "")
    p.right(right, 44, foot, 8.5, rgb=GREY)
    return _pdf("\n".join(p.ops), "Scrub-DICOM de-identification certificate", verified)


def certificate_for(out: Path) -> tuple[bytes, str] | None:
    """(PDF bytes, file name) for the newest PASS of this output tree; None when the output is not verified, or
    the newest check did not pass."""
    logs = Path(out) / "_logs"
    status, rep, _ = model.verify_status(logs) if logs.is_dir() else (None, None, "")
    att_path = model.latest_attestation(logs) if logs.is_dir() else None
    if status != "PASS" or not att_path or not rep:
        return None
    if att_path.stem.split("attestation_")[-1] != rep.stem.split("verify_")[-1]:
        return None                                    # the attestation belongs to an older check than the newest report
    try:
        att = json.loads(att_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    done, _partial = model.study_state(Path(out))
    stamp = att_path.stem.split("attestation_")[-1]
    return certificate_pdf(att, done, model.review_count(Path(out)), att_path.name), f"certificate_{stamp}.pdf"


def write_certificate(out: Path, dest: Path | None = None) -> Path | None:
    """Write the certificate into <out>/_logs (or to `dest`). Returns the path, or None when there is nothing to certify."""
    got = certificate_for(out)
    if not got:
        return None
    data, name = got
    path = Path(dest) if dest else Path(out) / "_logs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
