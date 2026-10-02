"""Folder-first intake: point the app at any folder of DICOM and learn what is in it. No Tk.

The guided flow starts here. `scan_folder` walks the folder, reads headers only, and returns the studies it
found (one per thing that will get a new ID) with their series, classified exactly as the engine's coronary rule
would classify them. The rest of the module turns the user's choices (which kinds of series, which new IDs) into
the three small files the engine already understands: a patient list, a series selection and a profile. The
engine itself is not changed and is not imported into the window's process for anything but its pure helpers.

Nothing here writes outside the folder it is given, and nothing is written at all until the caller asks.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pydicom

from scrubdicom.core import SERIES_TAGS, classify_series, is_review_object, safe_name, series_info

SCAN_TAGS = SERIES_TAGS + ["PatientID", "StudyInstanceUID", "StudyDate", "BurnedInAnnotation"]
SKIP_DIR_PREFIXES = ("_review", "_logs", "_to_delete", ".")                     # same as core.iter_dicom_files
SKIP_FILE_SUFFIXES = (".png", ".jpg", ".txt", ".csv", ".xlsx", ".json")           # same as core.iter_dicom_files
SAMPLE_OVER = 8          # a folder with more files than this is sampled (5 headers) before reading every file
PATIENT_LIST = "patient_list.csv"          # source_folder, study_id   (the engine's manifest)
PATIENT_ID_MAP = "patient_id_map.csv"      # current_id, new_id        (the engine's mapping, matched on Patient ID)
SELECTION = "series_selection.csv"         # study_id, series_uid, series_description
CHOICES = ("coronary", "all", "choose")
ID_RE = re.compile(r"[A-Za-z0-9._ -]+")


# ---------------------------------------------------------------------------------------------- what a scan finds

@dataclass
class FoundSeries:
    uid: str
    number: str
    description: str
    modality: str
    thickness: float | None
    n_images: int
    verdict: str            # keep | maybe | drop   (the coronary rule)
    reason: str
    review: str | None      # set when the engine would quarantine it for a human look
    sample: Path | None = None   # one file of the series (the middle one where known), for a thumbnail

    @property
    def kind(self) -> tuple:
        return (self.modality, re.sub(r"\s+", " ", self.description).strip().lower(),
                round(self.thickness, 2) if self.thickness is not None else None)


@dataclass
class Unit:
    """One thing that gets one new ID: a study, in its own folder (or, when patients share a folder, a Patient ID)."""
    key: str                # the original Patient ID ("" if the scans have none)
    folder: Path
    series: list[FoundSeries] = field(default_factory=list)
    study_date: str = ""
    new_id: str = ""

    @property
    def n_images(self) -> int:
        return sum(s.n_images for s in self.series)


@dataclass
class Intake:
    root: Path
    units: list[Unit] = field(default_factory=list)
    n_files: int = 0
    n_unreadable: int = 0
    mixed: bool = False               # several patients share a folder: the run matches on Patient ID, not on folder
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def n_patients(self) -> int:
        return len({u.key or str(u.folder) for u in self.units})

    @property
    def n_series(self) -> int:
        return sum(len(u.series) for u in self.units)

    def label(self, unit: Unit) -> str:
        """How a unit is shown to the user: its folder relative to the opened folder, or its Patient ID."""
        if self.mixed:
            return f"Patient ID {unit.key}"
        try:
            rel = unit.folder.relative_to(self.root)
        except ValueError:
            return unit.folder.name
        return str(rel) if rel.parts else self.root.name

    def noun(self, n: int) -> str:
        """'patients' when every study belongs to a different patient, 'studies' otherwise."""
        if self.n_patients == len(self.units):
            return "patient" if n == 1 else "patients"
        return "study" if n == 1 else "studies"

    def headline(self) -> str:
        if not self.units:
            return "No DICOM scans were found in this folder."
        s = len(self.units)
        p = self.n_patients
        who = f"{s} {'study' if s == 1 else 'studies'}" + (f" from {p} patients" if p != s else "")
        if p == s:
            who = f"{p} {'patient' if p == 1 else 'patients'}"
        return f"Found {who}: {self.n_series} series, {sum(u.n_images for u in self.units):,} images."


def _natural(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _read(path: Path):
    try:
        h = pydicom.dcmread(str(path), stop_before_pixels=True, specific_tags=SCAN_TAGS, force=True)
    except Exception:
        return None
    return h if "SOPClassUID" in h else None


def _ids(h) -> tuple[str, str, str]:
    return (str(h.get("PatientID", "") or "").strip(), str(h.get("StudyInstanceUID", "") or ""),
            str(h.get("SeriesInstanceUID", "") or "") or f"nouid-{h.get('SeriesNumber', 0)}")


def looks_like_output(d: Path) -> bool:
    """A folder this app wrote earlier: it has a _logs folder and finished-study markers. Never read as input."""
    try:
        if not (d / "_logs").is_dir():
            return False
        return any((c / ".complete").exists() or (c / ".complete_ctca").exists() for c in d.iterdir() if c.is_dir())
    except OSError:
        return False


def scan_folder(root: Path | str, progress=None, cancelled=None) -> Intake:
    """Read headers under `root` and group them into studies and series. `progress(n_files, n_series)` is called
    now and then; `cancelled()` returning True stops the walk and returns what was found so far.

    Folders that hold one series each (the usual PACS export) are sampled: five headers, and if they agree the
    rest are counted without being opened. Anything else is read file by file."""
    root = Path(root)
    it = Intake(root=root)
    recs: dict[tuple[str, str, str], dict] = {}
    try:
        root_is_dir = root.is_dir()
    except OSError:
        root_is_dir = False
    if not root_is_dir:
        it.problems.append("That folder could not be opened.")
        return it

    def add(h, count: int, d: Path, sample: Path, middle: bool = False) -> None:
        pid, study, series = _ids(h)
        rel = d.relative_to(root)
        gkey = pid or ("\0" + (rel.parts[0] if rel.parts else ""))     # no Patient ID: group by top-level sub-folder
        key = (gkey, study, series)
        r = recs.get(key)
        if r is None:
            r = recs[key] = {"head": h, "count": 0, "dirs": set(), "pid": pid, "sample": sample, "fs": None, "date": str(h.get("StudyDate", "") or "")}
        elif r["head"] is None:
            r["head"], r["fs"] = h, None       # the series carries on in another top-level folder: sum it up again at the end
        if middle:
            r["sample"] = sample
        r["count"] += count
        r["dirs"].add(d)
        it.n_files += count
        open_keys.add(key)

    # A header is about 9 KB in memory and a cohort has eight or so series per study: 16,000 studies would hold over a
    # gigabyte of them. So each top-level folder's series are summed up and their headers let go as soon as the walk
    # leaves that folder.
    open_keys: set = set()
    current_top: list = [None]

    def close_top() -> None:
        for key in open_keys:
            r = recs[key]
            if r["head"] is not None:
                r["fs"] = _found(r)
                r["head"] = None
        open_keys.clear()

    for dirpath, dirnames, filenames in os.walk(root):
        if cancelled and cancelled():
            it.cancelled = True
            break
        d = Path(dirpath)
        rel_parts = d.relative_to(root).parts
        top = rel_parts[0] if rel_parts else ""
        if top != current_top[0]:
            close_top()
            current_top[0] = top
        if d != root and looks_like_output(d):
            it.notes.append(f"Skipped '{d.name}': it is an earlier anonymised output.")
            dirnames[:] = []
            continue
        dirnames[:] = sorted((x for x in dirnames if not x.startswith(SKIP_DIR_PREFIXES)), key=_natural)
        names = sorted(fn for fn in filenames if not fn.startswith(".") and fn.upper() != "DICOMDIR"
                       and not fn.lower().endswith(SKIP_FILE_SUFFIXES))
        if not names:
            continue
        if len(names) > SAMPLE_OVER:
            n = len(names)
            heads = [_read(d / names[i]) for i in sorted({0, n // 4, n // 2, 3 * n // 4, n - 1})]
            if all(h is not None for h in heads) and len({_ids(h) for h in heads}) == 1:
                add(heads[0], n, d, d / names[n // 2], middle=True)
                if progress:
                    progress(it.n_files, len(recs))
                continue
        for k, fn in enumerate(names):
            h = _read(d / fn)
            if h is None:
                it.n_unreadable += 1
            else:
                add(h, 1, d, d / fn)
            if progress and k % 100 == 0:
                progress(it.n_files, len(recs))
                if cancelled and cancelled():
                    break
    close_top()
    _build_units(it, recs)
    return it


def _common(dirs) -> Path:
    return Path(os.path.commonpath([str(d) for d in dirs]))


def _nested(folders: list[Path]) -> bool:
    """True if any two of these folders are the same or one contains the other."""
    parts = sorted(f.parts for f in folders)
    return any(b[:len(a)] == a for a, b in zip(parts, parts[1:]))


def _found(r: dict) -> FoundSeries:
    """One series, classified by the engine's own rule from its header and its file count."""
    h, n = r["head"], r["count"]
    frames = int(h.get("NumberOfFrames", 0) or 0)
    thick = series_info(h)[0]
    verdict, reason = classify_series(h, n)
    return FoundSeries(uid=_ids(h)[2], number=str(h.get("SeriesNumber", "") or ""), description=str(h.get("SeriesDescription", "") or ""),
                       modality=str(h.get("Modality", "") or ""), thickness=thick, n_images=max(n, frames if n == 1 else 0),
                       verdict=verdict, reason=reason, review=is_review_object(h), sample=r.get("sample"))


def _series_of(rows: list[dict]) -> list[FoundSeries]:
    out = [r["fs"] or _found(r) for r in rows]
    out.sort(key=lambda s: (float(s.number) if s.number.replace(".", "", 1).isdigit() else 1e9, s.description))
    return out


def _build_units(it: Intake, recs: dict) -> None:
    by_patient: dict[str, dict[str, list[dict]]] = {}
    for (gkey, study, _series), r in recs.items():
        by_patient.setdefault(gkey, {}).setdefault(study, []).append(r)
    units: list[tuple[str, Unit]] = []
    for gkey, studies in by_patient.items():
        pid = next(iter(studies.values()))[0]["pid"]
        folders = {s: _common(set().union(*(r["dirs"] for r in rows))) for s, rows in studies.items()}
        if len(studies) > 1 and not _nested(list(folders.values())):
            for s, rows in studies.items():          # each study of this patient sits in its own folder: one ID each
                units.append((gkey, Unit(key=pid, folder=folders[s], series=_series_of(rows), study_date=rows[0]["date"])))
        else:
            rows = [r for rs in studies.values() for r in rs]
            units.append((gkey, Unit(key=pid, folder=_common(set().union(*(r["dirs"] for r in rows))), series=_series_of(rows),
                                     study_date=rows[0]["date"])))
    if len(by_patient) > 1 and _nested([u.folder for _, u in units]):
        # several patients' files share a folder: a folder can no longer stand for a patient, so match on Patient ID
        it.mixed = True
        merged: dict[str, Unit] = {}
        for gkey, u in units:
            m = merged.setdefault(gkey, Unit(key=u.key, folder=it.root, study_date=u.study_date))
            m.series += u.series
        units = list(merged.items())
        if any(not u.key for _, u in units):
            it.problems.append("Some scans have no Patient ID and share a folder with other patients, so they cannot be told apart. "
                               "Put each patient in a folder of their own and open the folder again.")
        it.notes.append("Several patients share a folder here, so each patient is recognised by the Patient ID inside the scans.")
    it.units = [u for _, u in units]
    it.units.sort(key=lambda u: (_natural(str(u.folder)), _natural(u.key), u.study_date))
    if it.n_unreadable:
        it.notes.append(f"{it.n_unreadable} files are not DICOM and will be ignored.")


# ---------------------------------------------------------------------------------------------- kinds of series

@dataclass
class Kind:
    """The same sort of series across the studies: modality + description + slice thickness."""
    key: tuple
    modality: str
    description: str
    thickness: float | None
    n_units: int = 0
    n_images: int = 0
    coronary: int = 0       # studies in which the coronary rule keeps it
    maybe: int = 0
    review: str | None = None
    reason: str = ""
    sample: Path | None = None      # a file to draw a thumbnail from: the largest series of this kind
    _sample_n: int = 0

    @property
    def note(self) -> str:
        if self.coronary and self.coronary == self.n_units:
            return "coronary CT"
        if self.coronary:
            return f"coronary CT in {self.coronary} of {self.n_units}"
        if self.review:
            if self.modality in ("SR", "DOC", "PR", "KO"):
                return "a report or note: held back for a look"
            return "held back for a look: may carry burned-in text"
        if self.maybe:
            return "thin CT, no contrast noted in the header"
        # everything else is described for what it is, not for why the coronary rule passes it over: most cohorts are not cardiac
        if "localiser" in self.reason:
            return "scout or reformat" + self._each
        return (f"{self.per_study:,} images each" if self.per_study > 1 else "a single image") if self.n_units else ""

    @property
    def per_study(self) -> int:
        return round(self.n_images / self.n_units) if self.n_units else 0

    @property
    def _each(self) -> str:
        return f", {self.per_study:,} images each" if self.per_study > 1 else ""

    def matches(self, query: str) -> bool:
        """Every word of the search appears somewhere in the type, the series name, the slice thickness or the note.
        'cta 1 mm' finds 1 mm CTA series; 'ct' alone finds everything CT."""
        hay = f"{self.modality} {self.description} {self.slice_text} {self.slice_text.replace(' ', '')} {self.note}".lower()
        return all(w in hay for w in query.lower().split())

    @property
    def slice_text(self) -> str:
        return f"{self.thickness:g} mm" if self.thickness is not None else ""


def series_kinds(it: Intake) -> list[Kind]:
    kinds: dict[tuple, Kind] = {}
    for u in it.units:
        seen = set()
        for s in u.series:
            k = kinds.setdefault(s.kind, Kind(key=s.kind, modality=s.modality, description=s.description, thickness=s.thickness, reason=s.reason))
            k.n_images += s.n_images
            if s.sample is not None and s.n_images > k._sample_n:
                k.sample, k._sample_n = s.sample, s.n_images
            if s.kind not in seen:
                seen.add(s.kind)
                k.n_units += 1
                k.coronary += s.verdict == "keep"
                k.maybe += s.verdict == "maybe"
            k.review = k.review or s.review
    return sorted(kinds.values(), key=lambda k: (-(k.coronary > 0), -(k.maybe > 0), k.review is not None, -k.n_units, k.modality, k.description.lower()))


def default_ticks(kinds: list[Kind]) -> set[tuple]:
    """What 'Let me choose' starts with: the coronary kinds if there are any, else everything."""
    cor = {k.key for k in kinds if k.coronary}
    return cor or {k.key for k in kinds if k.maybe} or {k.key for k in kinds}


def kept_series(unit: Unit, choice: str, ticked: set[tuple], override: set[str] | None = None) -> list[FoundSeries]:
    """The series of one study that the run will keep under this choice. `override` is that study's own ticks from
    the viewer, which win over everything."""
    if override is not None:
        return [s for s in unit.series if s.uid in override]
    if choice == "all":
        return list(unit.series)
    if choice == "choose":
        return [s for s in unit.series if s.kind in ticked]
    keep = [s for s in unit.series if s.verdict == "keep"]
    return keep or [s for s in unit.series if s.verdict == "maybe"]      # the engine's fallback, flagged 'needs a look'


@dataclass
class Plan:
    n_units: int
    n_series: int
    n_images: int
    empty: list[str]            # new IDs (or labels) of studies in which nothing would be kept

    def text(self, total_units: int, noun: str = "") -> str:
        if not self.n_series:
            return "Nothing would be kept. Tick at least one kind of series."
        noun = noun or ("study" if total_units == 1 else "studies")
        s = f"Keeps {self.n_series} series, {self.n_images:,} images, from {self.n_units} of {total_units} {noun}."
        if self.empty:
            s += f"  {len(self.empty)} would have nothing kept and are skipped."
        return s


def plan(it: Intake, choice: str, ticked: set[tuple], overrides: dict[str, set[str]] | None = None) -> Plan:
    overrides = overrides or {}
    n_units = n_series = n_images = 0
    empty = []
    for u in it.units:
        kept = kept_series(u, choice, ticked, overrides.get(u.new_id))
        if kept:
            n_units += 1
            n_series += len(kept)
            n_images += sum(s.n_images for s in kept)
        else:
            empty.append(u.new_id or it.label(u))
    return Plan(n_units, n_series, n_images, empty)


# ---------------------------------------------------------------------------------------------- new IDs

def match_key(it: Intake, unit: Unit) -> str:
    """What the engine will look a unit up by: its folder, or (shared folders) its Patient ID."""
    return unit.key if it.mixed else str(unit.folder)


def assign_ids(it: Intake, prefix: str = "ANON", existing: dict[str, str] | None = None, taken: set[str] | None = None) -> None:
    """Give every study a new ID: PREFIX-001, PREFIX-002... A study that already has an ID in `existing` (an earlier
    session's patient list) keeps it, so a second run over a grown folder never renumbers anyone; new studies are
    numbered after the highest number in use. `taken` are IDs already present in the output folder."""
    existing = existing or {}
    prefix = prefix.strip()
    used = {v.upper() for v in existing.values()} | {t.upper() for t in (taken or set())}
    stem = re.escape(prefix + "-") if prefix else ""
    nums = [int(m.group(1)) for v in used for m in [re.fullmatch(stem + r"(\d+)", v, re.I)] if m]
    n = max(nums, default=0)
    width = max(3, len(str(n + len(it.units))))
    for u in it.units:
        old = existing.get(match_key(it, u))
        if old:
            u.new_id = old
            continue
        while True:
            n += 1
            cand = f"{prefix}-{n:0{width}d}" if prefix else f"{n:0{width}d}"
            if cand.upper() not in used:
                break
        used.add(cand.upper())
        u.new_id = cand


def id_problems(it: Intake) -> list[str]:
    p: list[str] = []
    seen: dict[str, int] = {}
    for u in it.units:
        nid = u.new_id.strip()
        if not nid:
            p.append("Every study needs a new ID.")
            break
        if not ID_RE.fullmatch(nid):
            p.append(f"New ID '{nid}' may only contain letters, digits, spaces, '.', '_' and '-'.")
            break
        k = safe_name(nid).upper()
        seen[k] = seen.get(k, 0) + 1
    dup = sorted(k for k, c in seen.items() if c > 1)
    if dup:
        p.append(f"Two studies have the same new ID ({dup[0]}). Each needs its own.")
    return p


def apply_spreadsheet(it: Intake, mapping: dict[str, str]) -> int:
    """Fill new IDs from an old-ID -> new-ID table (keys upper-cased, as core.load_mapping returns them). The old ID
    may be the Patient ID inside the scans or the name of the study's folder. Returns how many studies matched."""
    hit = 0
    for u in it.units:
        for k in (u.key, u.folder.name, it.label(u)):
            v = mapping.get(str(k).strip().upper())
            if v:
                u.new_id = v
                hit += 1
                break
    return hit


# ---------------------------------------------------------------------------------------------- files for the engine

def read_patient_list(confidential: Path | str, mixed: bool = False) -> dict[str, str]:
    """An earlier session's assignments from the confidential folder: {folder or Patient ID: new ID}."""
    p = Path(confidential) / (PATIENT_ID_MAP if mixed else PATIENT_LIST)
    a, b = ("current_id", "new_id") if mixed else ("source_folder", "study_id")
    try:
        with open(p, newline="", encoding="utf-8-sig") as fh:
            return {(r.get(a) or "").strip(): (r.get(b) or "").strip() for r in csv.DictReader(fh) if (r.get(a) or "").strip() and (r.get(b) or "").strip()}
    except OSError:
        return {}


def write_patient_list(confidential: Path | str, it: Intake) -> Path:
    """The engine's patient list (or, for shared folders, its Patient-ID map), merged with what an earlier session
    wrote so studies anonymised before stay in the list. Lives in the confidential folder: it names the originals."""
    conf = Path(confidential)
    conf.mkdir(parents=True, exist_ok=True)
    rows = read_patient_list(conf, it.mixed)
    for u in it.units:
        rows[match_key(it, u)] = u.new_id.strip()
    p = conf / (PATIENT_ID_MAP if it.mixed else PATIENT_LIST)
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["current_id", "new_id"] if it.mixed else ["source_folder", "study_id"])
        w.writerows(sorted(rows.items(), key=lambda kv: _natural(kv[1])))
    return p


def run_list(confidential: Path | str, it: Intake) -> Path:
    """The list for THIS run: only the studies found in the opened folder, so a run never wanders off to folders
    from an earlier session. Written next to the cumulative list."""
    conf = Path(confidential)
    conf.mkdir(parents=True, exist_ok=True)
    p = conf / ("this_run_" + (PATIENT_ID_MAP if it.mixed else PATIENT_LIST))
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["current_id", "new_id"] if it.mixed else ["source_folder", "study_id"])
        w.writerows((match_key(it, u), u.new_id.strip()) for u in it.units)
    return p


def selection_rows(it: Intake, choice: str, ticked: set[tuple], overrides: dict[str, set[str]] | None = None) -> list[tuple[str, str, str]]:
    """(study_id, series_uid, description) rows for --select-series. With 'coronary' or 'all' on folder-per-study
    scans the engine decides by itself and only the viewer's per-study ticks are written; when the user chose the
    kinds, or patients share a folder (where the engine has no coronary switch), every study is spelled out."""
    overrides = overrides or {}
    explicit = choice == "choose" or (it.mixed and choice == "coronary")
    rows = []
    for u in it.units:
        ov = overrides.get(u.new_id)
        if ov is None and not explicit:
            continue
        rows += [(u.new_id.strip(), s.uid, s.description) for s in kept_series(u, choice, ticked, ov) if not s.uid.startswith("nouid-")]
    return rows


def write_selection(confidential: Path | str, rows: list[tuple[str, str, str]]) -> str:
    """Write (or, with no rows, remove) the series selection. Returns the path, or "" when there is none."""
    p = Path(confidential) / SELECTION
    if not rows:
        try:
            p.unlink()
        except OSError:
            pass
        return ""
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["study_id", "series_uid", "series_description"])
        w.writerows(rows)
    return str(p)


def suggest_folders(root: Path | str, documents: Path | None = None) -> tuple[str, str]:
    """Where the copies and the confidential material could go: two sibling folders next to the opened folder, or
    under Documents when that place cannot be written to (a CD, a read-only share). The names never include the
    opened folder's name, which is often a patient's name or hospital number."""
    root = Path(root)
    base = root.parent
    try:
        ok = base != root and base.is_dir() and os.access(str(base), os.W_OK)
    except OSError:
        ok = False
    if not ok:
        base = (documents or Path.home() / "Documents") / "Scrub-DICOM"
    out = base / "Anonymised scans"
    return str(out), str(base / "Anonymised scans_CONFIDENTIAL")
