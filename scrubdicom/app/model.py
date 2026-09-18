"""Everything the desktop app does that is not drawing widgets: building engine commands, reading the
output tree, parsing progress, persisting settings, and the share-readiness checks. No Tk imports, so all of
it is unit-testable without a display.

Security notes
- The app never opens a socket. Nothing here imports urllib, socket or http.
- Settings persist paths and booleans only (see SETTINGS_KEYS). Verify "needles" (surnames, hospital
  numbers) are deliberately not persisted.
- The whole `_logs` folder is treated as confidential, not just the LINKAGE file: files_*.csv, summary_*.csv,
  unmapped_*.csv and the app/dashboard run logs all carry original folder paths, and series_*.csv carries
  original series descriptions. Only uid_salt.txt stays behind when logs are moved out.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from scrubdicom.core import VERSION as ENGINE_VERSION, MAX_SLICE_MM

APP_NAME = "Scrub-DICOM"
APP_VERSION = ENGINE_VERSION

# ---------------------------------------------------------------------------------------------- environment


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def engine_command(args: list[str]) -> list[str]:
    """The exact command line used to run the engine as a child process. Frozen: re-enter our own executable
    with --cli (see scrubdicom.app.main). From source: the module CLI, unbuffered, warnings silenced as the
    launchers do."""
    if is_frozen():
        return [sys.executable, "--cli", *args]
    return [sys.executable, "-u", "-W", "ignore", "-m", "scrubdicom", *args]


def settings_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "scrub-dicom"
    return base / "settings.json"


# ---------------------------------------------------------------------------------------------- job spec

MODES = ("manifest", "single", "mapping")
MODE_LABELS = {
    "manifest": "Cohort manifest CSV",
    "single": "One patient folder",
    "mapping": "Folder of patients + mapping file",
}
MODE_HELP = {
    "manifest": "A CSV with columns source_folder, study_id: one patient folder per row, anywhere on any drive. Supports Resume and coronary-only.",
    "single": "Everything under one folder gets the study ID you type. Good for a single case.",
    "mapping": "One folder holding a sub-folder per patient, plus a CSV/XLSX mapping the current ID (or folder name) to the study ID.",
}
MANIFEST_ONLY_OPTIONS = ("ctca_only", "resume", "series_pick", "remap")


@dataclass
class JobSpec:
    """One `run` invocation, as the form describes it. `run_args()` turns it into CLI arguments and is the
    single place where form fields become flags, so the tests can pin the mapping."""
    mode: str = "manifest"
    output: str = ""
    manifest: str = ""
    remap: str = ""
    input: str = ""
    study_id: str = ""
    mapping: str = ""
    current_col: str = ""
    new_col: str = ""
    sheet: str = ""
    match_on: str = "patientid"
    series_pick: str = ""
    ctca_only: bool = True
    resume: bool = True
    keep_technical: bool = False
    flat: bool = False
    dry_run: bool = False

    def validate(self) -> list[str]:
        p: list[str] = []
        if self.mode not in MODES:
            p.append(f"Unknown mode {self.mode!r}")
        if not self.output.strip():
            p.append("Choose an output folder.")
        if self.mode == "manifest":
            if not self.manifest.strip():
                p.append("Choose the manifest CSV.")
            elif not Path(self.manifest).is_file():
                p.append(f"Manifest not found: {self.manifest}")
            if self.remap.strip() and "=" not in self.remap:
                p.append("Path remap must look like OLDPREFIX=NEWPREFIX, e.g. /Volumes/Drive=E:\\")
            if self.series_pick.strip() and not Path(self.series_pick).is_file():
                p.append(f"Series-pick file not found: {self.series_pick}")
        else:
            if not self.input.strip():
                p.append("Choose the input folder.")
            elif not Path(self.input).is_dir():
                p.append(f"Input folder not found: {self.input}")
            if self.mode == "single" and not self.study_id.strip():
                p.append("Give the study ID to apply to this folder.")
            if self.mode == "single" and self.study_id.strip() and not re.fullmatch(r"[A-Za-z0-9._-]+", self.study_id.strip()):
                p.append("Study ID may only contain letters, digits, '.', '_' and '-'.")
            if self.mode == "mapping":
                if not self.mapping.strip():
                    p.append("Choose the mapping file (CSV or XLSX).")
                elif not Path(self.mapping).is_file():
                    p.append(f"Mapping file not found: {self.mapping}")
        if self.output.strip() and self.mode != "manifest" and self.input.strip():
            try:
                out, inp = Path(self.output).resolve(), Path(self.input).resolve()
                if out == inp:
                    p.append("Output folder must not be the input folder.")
                elif inp in out.parents:
                    p.append("Output folder is inside the input folder; choose a separate location.")
            except OSError:
                pass
        return p

    def run_args(self) -> list[str]:
        a: list[str] = []
        if self.mode == "manifest":
            a += ["--manifest", self.manifest.strip()]
            if self.remap.strip():
                a += ["--remap", self.remap.strip()]
            if self.resume:
                a.append("--resume")
            if self.ctca_only:
                a.append("--ctca-only")
            if self.series_pick.strip():
                a += ["--series-pick", self.series_pick.strip()]
        else:
            a += ["--input", self.input.strip()]
            if self.mode == "single":
                a += ["--study-id", self.study_id.strip()]
            else:
                a += ["--mapping", self.mapping.strip()]
                if self.current_col.strip():
                    a += ["--current-col", self.current_col.strip()]
                if self.new_col.strip():
                    a += ["--new-col", self.new_col.strip()]
                if self.sheet.strip():
                    a += ["--sheet", self.sheet.strip()]
                a += ["--match-on", self.match_on]
        a += ["--output", self.output.strip()]
        if self.keep_technical:
            a.append("--keep-technical")
        if self.flat:
            a.append("--flat")
        if self.dry_run:
            a.append("--dry-run")
        return a

    def command(self) -> list[str]:
        return engine_command(["run", *self.run_args()])


def verify_args(output: str, needles: list[str], keep_technical: bool = False) -> list[str]:
    a = ["--output", output.strip()]
    for n in needles:
        n = n.strip()
        if n:
            a += ["--needle", n]
    if keep_technical:
        a.append("--keep-technical")
    return a


def thick_args(output: str, fix: bool) -> list[str]:
    return ["--output", output.strip()] + (["--fix"] if fix else [])


def split_needles(text: str) -> list[str]:
    """'Smith, Jones;  Bloggs' -> ['Smith', 'Jones', 'Bloggs']."""
    return [n.strip() for n in re.split(r"[,;\n]+", text or "") if n.strip()]


# ---------------------------------------------------------------------------------------------- progress

PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\]\s+(\S+)")
ETA_RE = re.compile(r"~(\d+) min left")


@dataclass
class Progress:
    done: int
    total: int
    study: str
    percent: int
    eta_minutes: int | None = None

    @property
    def text(self) -> str:
        s = f"Study {self.done} of {self.total} ({self.percent}%): {self.study}"
        if self.eta_minutes is not None:
            s += f", about {self.eta_minutes} min left"
        return s


def parse_progress(line: str) -> Progress | None:
    m = PROGRESS_RE.match(line.strip())
    if not m:
        return None
    done, total, study = int(m.group(1)), int(m.group(2)), m.group(3)
    eta = ETA_RE.search(line)
    return Progress(done, total, study, done * 100 // max(total, 1), int(eta.group(1)) if eta else None)


def is_skipped_line(line: str) -> bool:
    """Lines for studies that --resume skipped; hidden from the live log by default because on a long resume
    there are hundreds of them."""
    return "already complete, skipped" in line


def classify_line(line: str) -> str:
    """'error' | 'warn' | 'ok' | 'plain', for colouring the live log."""
    s = line.strip()
    if not s:
        return "plain"
    if s.startswith("**") or "ERROR" in s or s.startswith("FAIL") or "Traceback" in s or "not found" in s:
        return "error"
    if "CHECK" in s or "NO CORONARY" in s or "NO DICOM" in s or "UNMAPPED" in s or "WARN" in s.upper():
        return "warn"
    if s.startswith("PASS") or s.startswith("Done in") or s.startswith("DRY RUN"):
        return "ok"
    return "plain"


# ---------------------------------------------------------------------------------------------- output tree


def read_csv(p: Path) -> list[dict]:
    try:
        with open(p, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    except (OSError, UnicodeDecodeError):
        return []


def manifest_count(path: str) -> int | None:
    p = Path(path) if path else None
    if not p or not p.is_file():
        return None
    return sum(1 for r in read_csv(p) if (r.get("source_folder") or "").strip() and (r.get("study_id") or "").strip())


def study_state(out: Path) -> tuple[list[str], list[str]]:
    """(complete, half-finished) study folder names under the output tree."""
    done, partial = [], []
    try:
        if out.is_dir():
            for p in sorted(out.iterdir()):
                if p.is_dir() and not p.name.startswith(("_", ".")):
                    (done if any(p.glob(".complete*")) else partial).append(p.name)
    except OSError:
        pass
    return done, partial


def latest(logs: Path, prefix: str, suffixes: tuple[str, ...] = (".csv", ".txt")) -> Path | None:
    try:
        files = [p for s in suffixes for p in logs.glob(f"{prefix}_*{s}")]
    except OSError:
        return None
    files.sort(key=lambda p: p.name)
    return files[-1] if files else None


def load_series_rows(logs: Path) -> list[dict]:
    """All series decisions across every run, newest run winning for a repeated (study, series)."""
    latest_by_key: dict[tuple[str, str, str], dict] = {}
    try:
        files = sorted(logs.glob("series_*.csv"), key=lambda p: p.name)
    except OSError:
        return []
    for f in files:
        run = f.stem.replace("series_", "")
        for r in read_csv(f):
            r = dict(r)
            r["run"] = run
            key = (r.get("study_id", ""), r.get("series_number", ""), r.get("original_description", ""))
            latest_by_key[key] = r
    rows = list(latest_by_key.values())
    rows.sort(key=lambda r: (r.get("study_id", ""), _num(r.get("series_number", ""))))
    return rows


def _num(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 1e9


def needs_check(row: dict) -> bool:
    reason = row.get("reason", "") or ""
    return "CHECK" in reason or "fallback" in reason.lower()


SERIES_FILTERS = ("All", "Kept", "Dropped", "Needs a look")


def filter_series(rows: list[dict], mode: str, query: str = "") -> list[dict]:
    if mode == "Kept":
        rows = [r for r in rows if r.get("decision") == "keep"]
    elif mode == "Dropped":
        rows = [r for r in rows if r.get("decision") == "drop"]
    elif mode == "Needs a look":
        rows = [r for r in rows if needs_check(r)]
    q = (query or "").strip().lower()
    if q:
        rows = [r for r in rows if q in (r.get("study_id", "") or "").lower()]
    return rows


def series_counts(rows: list[dict]) -> dict[str, int]:
    return {
        "studies": len({r.get("study_id") for r in rows}),
        "kept": sum(1 for r in rows if r.get("decision") == "keep"),
        "dropped": sum(1 for r in rows if r.get("decision") == "drop"),
        "check": sum(1 for r in rows if needs_check(r)),
    }


def write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    columns = columns or (list(rows[0].keys()) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def verify_status(logs: Path) -> tuple[str | None, Path | None, str]:
    """('PASS'|'FAIL'|None, report path, report text) for the newest verify report."""
    rep = latest(logs, "verify", (".txt",))
    if not rep:
        return None, None, ""
    try:
        txt = rep.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, rep, ""
    status = None
    for line in txt.splitlines():
        if line.startswith("PASS"):
            status = "PASS"
            break
        if line.startswith("FAIL"):
            status = "FAIL"
            break
    return status, rep, txt


# ---------------------------------------------------------------------------------------------- sharing

LOG_KEEP = {"uid_salt.txt"}   # the only _logs file that is not confidential and is needed by re-runs


@dataclass
class Check:
    level: str      # "ok" | "warn" | "block"
    title: str
    detail: str = ""


def is_pass_report(p: Path) -> bool:
    """A PASS verify report names only the output folder and a file count; it stays as proof of verification.
    A FAIL report quotes the offending values, so it is confidential."""
    if not p.name.startswith("verify_") or p.suffix != ".txt":
        return False
    try:
        return any(line.startswith("PASS") for line in p.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return False


def confidential_log_files(logs: Path) -> list[Path]:
    try:
        return sorted(p for p in logs.iterdir()
                      if p.is_file() and p.name not in LOG_KEEP and not p.name.startswith(".") and not is_pass_report(p))
    except OSError:
        return []


def linkage_files(out: Path) -> list[Path]:
    """LINKAGE_*_CONFIDENTIAL.csv anywhere under the output tree, wherever the user may have put one."""
    found = []
    try:
        for dirpath, dirnames, filenames in os.walk(out):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            found += [Path(dirpath) / f for f in filenames if f.upper().startswith("LINKAGE_") and f.upper().endswith("_CONFIDENTIAL.CSV")]
    except OSError:
        pass
    return sorted(found)


def review_count(out: Path) -> int:
    try:
        return sum(1 for p in (out / "_review").rglob("*") if p.is_file() and not p.name.startswith("."))
    except OSError:
        return 0


def _mtime(p: Path | None) -> float:
    try:
        return p.stat().st_mtime if p else 0.0
    except OSError:
        return 0.0


def share_readiness(out: Path) -> list[Check]:
    """What stands between this output tree and handing it to a reader. 'block' items must be fixed; 'warn'
    items need a decision by a human."""
    checks: list[Check] = []
    if not out.is_dir():
        return [Check("block", "Output folder does not exist", str(out))]
    logs = out / "_logs"
    done, partial = study_state(out)
    status, rep, _ = verify_status(logs)
    last_run = max((_mtime(latest(logs, "summary", (".csv",))), _mtime(latest(logs, "files", (".csv",)))))
    if status == "PASS":
        if rep and _mtime(rep) < last_run:
            checks.append(Check("warn", "Verify passed, but a run has happened since", "Run verify again so the report covers the newest studies."))
        else:
            checks.append(Check("ok", "Verify passed", rep.name if rep else ""))
    elif status == "FAIL":
        checks.append(Check("block", "Verify FAILED", f"Read {rep.name if rep else 'the report'} on the Verify tab; do not share until it passes."))
    else:
        checks.append(Check("block", "Not verified", "Run verify before sharing anything."))
    if not done:
        checks.append(Check("block", "No completed studies", "Nothing to share yet."))
    else:
        checks.append(Check("ok", f"{len(done)} completed studies"))
    if partial:
        checks.append(Check("warn", f"{len(partial)} half-finished studies", "They will be redone on the next resume; do not share them: " + ", ".join(partial[:10]) + (" ..." if len(partial) > 10 else "")))
    n_rev = review_count(out)
    if n_rev:
        checks.append(Check("warn", f"{n_rev} quarantined files in _review", "Secondary captures, reports and PDFs: headers are clean but pixels may carry burned-in text. Look at them; do not share unchecked."))
    link = linkage_files(out)
    if link:
        checks.append(Check("block", f"{len(link)} linkage file(s) still in the output tree", "Each links study IDs to real patients. Use 'Move confidential logs out'."))
    else:
        checks.append(Check("ok", "No linkage file in the output tree"))
    conf = confidential_log_files(logs)
    conf_other = [p for p in conf if p not in link]
    if conf_other:
        checks.append(Check("warn", f"{len(conf_other)} other log file(s) in _logs carry original folder paths or descriptions",
                            "files_*, summary_*, unmapped_*, series_* and the run logs. Move the whole _logs folder out; only uid_salt.txt needs to stay."))
    return checks


def move_logs_out(out: Path, dest_parent: Path) -> tuple[list[Path], Path]:
    """Move every confidential file from <out>/_logs into a new, timestamped folder under dest_parent. Refuses a
    destination inside the output tree. uid_salt.txt stays so re-runs keep the same UIDs. Returns (moved, folder)."""
    out, dest_parent = Path(out).resolve(), Path(dest_parent).resolve()
    if dest_parent == out or out in dest_parent.parents:
        raise ValueError("The destination is inside the output folder; choose somewhere outside it, ideally a different drive or an encrypted folder.")
    logs = out / "_logs"
    files = confidential_log_files(logs) + [p for p in linkage_files(out) if p.parent != logs]
    if not files:
        raise ValueError("Nothing to move: _logs holds only uid_salt.txt.")
    folder = dest_parent / f"{out.name}_logs_CONFIDENTIAL_{time.strftime('%Y%m%d_%H%M%S')}"
    folder.mkdir(parents=True, exist_ok=False)
    moved = []
    for p in files:
        target = folder / p.name
        if target.exists():
            target = folder / f"{p.stem}_{len(moved)}{p.suffix}"
        shutil.move(str(p), str(target))
        moved.append(target)
    return moved, folder


# ---------------------------------------------------------------------------------------------- settings

SETTINGS_KEYS = {
    # paths the user chose, and booleans. Nothing that identifies a patient is ever written here.
    "mode", "output", "manifest", "remap", "input", "mapping", "current_col", "new_col", "sheet", "match_on",
    "series_pick", "ctca_only", "resume", "keep_technical", "flat", "verify_after_run", "show_all_lines", "geometry",
}
_DEFAULTS = {
    "mode": "manifest", "output": "", "manifest": "", "remap": "", "input": "", "mapping": "", "current_col": "",
    "new_col": "", "sheet": "", "match_on": "patientid", "series_pick": "", "ctca_only": True, "resume": True,
    "keep_technical": False, "flat": False, "verify_after_run": True, "show_all_lines": False, "geometry": "",
}


@dataclass
class Settings:
    values: dict = field(default_factory=lambda: dict(_DEFAULTS))
    path: Path = field(default_factory=settings_path)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        s = cls(path=path or settings_path())
        try:
            raw = json.loads(s.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return s
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in SETTINGS_KEYS and isinstance(v, type(_DEFAULTS[k])):
                    s.values[k] = v
        return s

    def save(self) -> None:
        clean = {k: v for k, v in self.values.items() if k in SETTINGS_KEYS}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(clean, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def get(self, k: str):
        return self.values.get(k, _DEFAULTS.get(k))

    def set(self, k: str, v) -> None:
        if k in SETTINGS_KEYS:
            self.values[k] = v

    def spec(self) -> JobSpec:
        v = self.values
        return JobSpec(mode=v["mode"], output=v["output"], manifest=v["manifest"], remap=v["remap"], input=v["input"],
                       mapping=v["mapping"], current_col=v["current_col"], new_col=v["new_col"], sheet=v["sheet"],
                       match_on=v["match_on"], series_pick=v["series_pick"], ctca_only=v["ctca_only"], resume=v["resume"],
                       keep_technical=v["keep_technical"], flat=v["flat"])


def spec_to_settings(spec: JobSpec, settings: Settings) -> None:
    for k, v in asdict(spec).items():
        if k in SETTINGS_KEYS and k != "study_id":
            settings.set(k, v)


def about_text() -> str:
    return (f"{APP_NAME} {APP_VERSION}\n"
            f"Engine: scrubdicom.core {ENGINE_VERSION}   ·   Python {sys.version.split()[0]}   ·   "
            f"{'packaged app' if is_frozen() else 'running from source'}\n"
            f"Coronary series rule: slices <= {MAX_SLICE_MM:g} mm\n\n"
            "Built by Charles Badu-Boateng. Copyright BB & Co Holdings Ltd. PolyForm Noncommercial 1.0.0.\n"
            "This app never connects to the internet and never modifies the original scans.")
