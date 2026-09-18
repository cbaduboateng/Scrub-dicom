#!/usr/bin/env python3
"""
series_survey.py - inventory every series in a set of patient folders and classify which ones are the
coronary CT angiogram (CTCA) worth keeping for a reader study.

  python series_survey.py survey  <root_with_patient_folders> --out survey.csv
  python series_survey.py select  <root_with_patient_folders> --survey survey.csv --dest "<root>/selected dicom scans"

The survey writes one row per series with a proposed decision (keep / drop / check) and a reason.
Edit the 'decision' column if you disagree, then run 'select' to copy only the kept series
(originals untouched) into <dest>/<patient>/<series folder>/.

Classification heuristics (vendor-agnostic, header only):
  drop   non-CT objects (SR, PR, KO, SC, dose reports), localisers/topograms/scouts, series with < 40
         images, MPR/derived/secondary images, lung or bone kernels, non-contrast calcium score runs,
         slice thickness > 1.5 mm, obvious non-cardiac exams (aorta/chest/abdomen/CTPA by description)
  keep   ORIGINAL/PRIMARY axial CT, thin slices (<= 1.5 mm), >= 40 images, contrast given or
         description suggests CTA/coronary, no exclusion keyword
  check  anything ambiguous (e.g. no description, contrast field empty, multiple candidate phases)
"""
from __future__ import annotations
import argparse, csv, os, re, shutil, sys
from collections import defaultdict
from pathlib import Path
import pydicom

TAGS = ["SeriesInstanceUID", "SeriesNumber", "SeriesDescription", "Modality", "SOPClassUID", "ImageType",
        "SliceThickness", "KVP", "ContrastBolusAgent", "ConvolutionKernel", "Rows", "Columns", "StudyDate",
        "StudyDescription", "ProtocolName", "PixelSpacing", "ReconstructionDiameter", "BodyPartExamined",
        "InstanceNumber", "NumberOfFrames"]

DROP_WORDS = ["topogram", "scout", "localizer", "localiser", "surview", "lung", "bone", "calcium", "ca score",
              "cascore", "ca-score", "score", "non contrast", "noncontrast", "nc ", "mpr", "snapshot", "cpr", "curved",
              "vr ", "volume render", "mip", "dose", "report", "patient protocol", "monitoring", "bolus track",
              "test bolus", "premonitor", "timing", "pulmonary", "ctpa", "abdomen", "pelvis", "head", "brain",
              "delayed", "late phase", "sub", "secondary", "screen", "capture", "aorta", "aortic", "chest",
              "thorax", "3d", "reformat", "sagittal", "coronal", "fai", "caristo", "epicardial", "pericoronary",
              "b60", "b70", "b80", "i70", "i50", "lung kernel", "hr40", "fc51", "fc52", "fc85", "fc86", "fc30"]
KEEP_WORDS = ["cta", "ctca", "coronary", "coro", "angio", "cardiac", "heart", "diastol", "systol", "75%", "70%",
              "78%", "80%", "40%", "45%", "best phase", "vascular", "step", "flash", "sequential", "retro", "prospective"]
NONCT_SOP = ("1.2.840.10008.5.1.4.1.1.7", "1.2.840.10008.5.1.4.1.1.88", "1.2.840.10008.5.1.4.1.1.11", "1.2.840.10008.5.1.4.1.1.104")


def iter_files(folder: Path):
    for dp, dn, fn in os.walk(folder):
        dn[:] = [d for d in dn if not d.startswith((".", "_"))]
        for f in fn:
            if f.startswith(".") or f.upper() == "DICOMDIR" or f.lower().endswith((".png", ".jpg", ".txt", ".csv", ".xlsx", ".json", ".zip", ".pdf")):
                continue
            yield Path(dp) / f


def survey(root: Path, patients: list[str] | None):
    rows = []
    folders = [p for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith((".", "_")) and p.name != "selected dicom scans"]
    if patients:
        folders = [p for p in folders if p.name in patients]
    for pf in folders:
        series = defaultdict(lambda: {"files": [], "hdr": None, "insts": set()})
        n_files = 0
        for f in iter_files(pf):
            try:
                ds = pydicom.dcmread(str(f), stop_before_pixels=True, specific_tags=TAGS, force=True)
            except Exception:
                continue
            if "SOPClassUID" not in ds:
                continue
            n_files += 1
            uid = str(ds.get("SeriesInstanceUID", "")) or f"nouid-{ds.get('SeriesNumber', 0)}"
            s = series[uid]
            s["files"].append(f)
            s["insts"].add(str(ds.get("InstanceNumber", "")))
            if s["hdr"] is None:
                s["hdr"] = ds
        print(f"{pf.name}: {n_files} DICOM files, {len(series)} series", flush=True)
        cands = []
        for uid, s in series.items():
            h = s["hdr"]
            desc = str(h.get("SeriesDescription", "") or "")
            itype = "\\".join(str(x) for x in (h.get("ImageType", []) or []))
            mod = str(h.get("Modality", ""))
            sop = str(h.get("SOPClassUID", ""))
            thick = h.get("SliceThickness", None)
            try:
                thick = float(thick) if thick not in (None, "") else None
            except Exception:
                thick = None
            n = len(s["files"])
            frames = h.get("NumberOfFrames", None)
            contrast = str(h.get("ContrastBolusAgent", "") or "")
            kernel = str(h.get("ConvolutionKernel", "") or "")
            dl = (desc + " " + kernel).lower()
            reason, dec = [], "keep"
            if mod != "CT" or sop.startswith(NONCT_SOP):
                dec, reason = "drop", [f"not a CT image object ({mod})"]
            elif "LOCALIZER" in itype.upper() or any(w in dl for w in ("topogram", "scout", "localizer", "localiser", "surview")):
                dec, reason = "drop", ["localiser/topogram"]
            elif "SECONDARY" in itype.upper() or "DERIVED" in itype.upper() or "MPR" in itype.upper():
                dec, reason = "drop", [f"derived/secondary image ({itype})"]
            elif n < 40 and not (frames and int(frames) >= 40):
                dec, reason = "drop", [f"only {n} images"]
            elif thick is not None and thick > 1.5:
                dec, reason = "drop", [f"slice {thick} mm"]
            else:
                hits = [w for w in DROP_WORDS if w in dl]
                if hits:
                    dec, reason = "drop", [f"description/kernel: {', '.join(hits[:3])}"]
                else:
                    kh = [w for w in KEEP_WORDS if w in dl]
                    if not contrast and not kh:
                        dec, reason = "check", ["no contrast field and no CTA keyword in description"]
                    elif not kh and not desc:
                        dec, reason = "check", ["no series description"]
                    else:
                        reason = [f"thin axial CT, {n} images" + (f", contrast {contrast[:20]}" if contrast else "") + (f", '{kh[0]}'" if kh else "")]
            row = {"patient": pf.name, "series_number": str(h.get("SeriesNumber", "")), "description": desc[:60],
                   "image_type": itype[:40], "modality": mod, "images": n, "slice_mm": thick if thick is not None else "",
                   "kvp": str(h.get("KVP", "")), "contrast": contrast[:25], "kernel": kernel[:12],
                   "matrix": f"{h.get('Rows', '')}x{h.get('Columns', '')}", "decision": dec, "reason": "; ".join(reason),
                   "series_uid": uid, "example_file": str(s["files"][0].relative_to(root))}
            rows.append(row)
            if dec == "keep":
                cands.append(row)
        if not cands:
            for r in rows:
                if r["patient"] == pf.name and r["decision"] == "check":
                    r["reason"] += " [NO keep candidate in this study]"
            if not any(r["patient"] == pf.name and r["decision"] == "check" for r in rows):
                print(f"   ** {pf.name}: no CTCA candidate found - review by hand **")
    return rows


def cmd_survey(a):
    root = Path(a.root)
    rows = survey(root, a.patient)
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"\nWrote {len(rows)} series rows to {a.out}")


def cmd_select(a):
    root, dest = Path(a.root), Path(a.dest)
    rows = list(csv.DictReader(open(a.survey, newline="")))
    keep = {(r["patient"], r["series_uid"]) for r in rows if r["decision"].strip().lower() == "keep"}
    per_pat = defaultdict(int)
    for pf in sorted(root.iterdir()):
        if not pf.is_dir() or pf.name.startswith((".", "_")) or pf == dest:
            continue
        if not any(p == pf.name for p, _ in keep):
            continue
        n = 0
        for f in iter_files(pf):
            try:
                ds = pydicom.dcmread(str(f), stop_before_pixels=True, specific_tags=["SeriesInstanceUID", "SeriesNumber"], force=True)
            except Exception:
                continue
            uid = str(ds.get("SeriesInstanceUID", "")) or f"nouid-{ds.get('SeriesNumber', 0)}"
            if (pf.name, uid) not in keep:
                continue
            try:
                sno = int(float(str(ds.get("SeriesNumber", "0") or 0)))
            except ValueError:
                sno = 0
            d = dest / pf.name / f"S{sno:03d}"
            d.mkdir(parents=True, exist_ok=True)
            target = d / f.name
            if target.exists():
                target = d / f"{f.stem}_{n}{f.suffix}"
            shutil.copy2(f, target)
            n += 1
        per_pat[pf.name] = n
        print(f"{pf.name}: {n} files copied", flush=True)
    print(f"\nDone: {sum(per_pat.values())} files for {len(per_pat)} patients under {dest}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("survey"); s.add_argument("root"); s.add_argument("--out", default="series_survey.csv")
    s.add_argument("--patient", action="append", help="limit to these patient folder names (repeatable)")
    s.set_defaults(func=cmd_survey)
    c = sub.add_parser("select"); c.add_argument("root"); c.add_argument("--survey", required=True); c.add_argument("--dest", required=True)
    c.set_defaults(func=cmd_select)
    a = p.parse_args(); a.func(a)


if __name__ == "__main__":
    main()
