"""The Scrub-DICOM window. Tkinter/ttk only: no web server, no port, no third-party UI dependency beyond the theme.

Designed so that at every moment there is one obvious next action and no unsafe one:
- a guided three-step flow (scans, output, review) with Next disabled until the step is complete;
- Anonymise stays disabled until Preview has run for the same settings;
- one status strip under the header on every tab; an orange banner whenever the profile retains anything;
- hand-over is gated on a passed check and no linkage file in the output tree;
- destructive actions need the word YES typed;
- empty tabs say what to do next; failures show a recovery card with one button;
- a built-in two-patient demo on first launch.

All engine work happens in a child process (runner.py) and the window polls it four times a second. Anything that
is logic rather than widgets lives in model.py and is tested there.
"""
from __future__ import annotations

import contextlib
import gc
import io
import os
import shlex
import subprocess
import shutil
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import brand
from . import certificate
from . import dnd
from . import intake
from . import model
from . import plain
from . import preview as pv
from . import theme
from .model import APP_NAME, APP_VERSION, JobSpec, Settings
from .plain import KEPT_WORDS
from .profile_ui import ProfileEditor
from scrubdicom.profiles import Profile
from .runner import EngineProcess
from .viewer import open_viewer

POLL_MS = 250
MONO = ("Menlo", 11) if sys.platform == "darwin" else (("Consolas", 10) if os.name == "nt" else ("TkFixedFont", 10))
LEVEL_MARK = {"ok": "\u2713", "warn": "!", "block": "\u2715"}
MATCH_ON_LABELS = {"patientid": "Patient ID in the scans", "folder": "sub-folder name"}
MATCH_ON_KEYS = {v: k for k, v in MATCH_ON_LABELS.items()}
STEP_TITLES = ("Open your scans", "Which scans do you want?", "What should be removed?", "Save and go", "Done")
STEP_SHORT = ("Open", "Scans", "Remove", "Save", "Done")
SAVE_STEP, DONE_STEP = 3, 4
THUMB = 40          # pixels: the picture beside each kind of series on step 2
LIST_MODES = ("manifest", "mapping")      # the methods behind "I already have a patient list"; one patient = open its folder
WRAP = 900
SERIES_CHOICES = (
    ("coronary", "Coronary CT only", "the thin contrast series a coronary read needs; scouts, calcium scores, X-rays and reports are left out"),
    ("all", "Everything", "every series in the folder is anonymised"),
    ("choose", "Let me choose", "tick the kinds of series to keep in the list below"),
)
KEEP_OPTIONS = (
    ("keep_sex", "Sex", ""),
    ("keep_age_5y", "Age, as a 5-year band", "e.g. 55 to 59"),
    ("keep_weight_height", "Weight and height", ""),
    ("shift_dates", "Dates, moved by a secret number of days", "gaps stay true"),
    ("keep_manufacturer", "Scanner make and model", "may hint at the hospital"),
    ("keep_technical", "Scanner technical details", "kernel, scan options"),
    ("keep_institution", "Hospital name", "names the centre"),
)
CUSTOM_PROFILE = "chosen_in_the_app.json"
DEST_PROBLEMS = ("Choose an output folder", "Choose a confidential folder", "The confidential folder must be outside",
                 "Output folder must not be", "Output folder is inside")
HELP_STEPS = (
    ("Open", "Choose the folder that holds the scans. One patient or a whole cohort."),
    ("Choose scans", "Coronary CT only, everything, or tick the kinds of series you want."),
    ("What to remove", "Everything identifying, or keep a few things a study needs."),
    ("Save", "Copies go to a new folder, then every file is re-read for identifiers."),
    ("Done", "See the result, open the certificate, hand over."),
)
HELP_QA = (
    ("Are my original scans changed?",
     "No. The app only reads them. The anonymised copies are written to a separate folder that you choose."),
    ("What exactly is removed?",
     "Names, dates of birth, hospital and NHS numbers, addresses, doctors' names, accession numbers, comments, the real dates, the hospital, "
     "the scanner identity, private vendor tags and the original UIDs.\n\nStep 3 lets a study keep a few things, such as sex or an age band, when it needs them."),
    ("How do I keep only the coronary series?",
     "On step 2 choose 'Coronary CT only'.\n\nTo pick by hand, choose 'Let me choose' and tick the kinds of series you want. "
     "'Look at the images' opens the viewer if you would like to see them first, and lets you tick series for one patient."),
    ("What is the confidential folder?",
     "It holds the key that links each new ID to the real patient, together with the logs. Keep it yourself and never send it with the scans.\n\n"
     "Without it, nobody can work out who a scan belongs to."),
    ("How do I know it worked?",
     "After anonymising, the app re-opens every output file and searches it for anything identifying. The run ends on the Done step: "
     "a green tick and 'anonymised and verified' means every file was found clean, and the strip at the top says 'Verified. Safe to hand over.'\n\n"
     "Do not share anything before it does. 'Certificate (PDF)' gives you a one-page record to keep."),
    ("Some files were held back. Why?",
     "X-rays, ultrasound, screenshots and reports often have the patient's name burned into the picture itself. Those are set aside in a "
     "_review folder for you to look at. The viewer can black out the text and release them."),
    ("Can I stop and carry on later?",
     "Yes. Press Stop. Next time, open the same folder and keep the same two destination folders: the app gives every patient the same ID "
     "as before and carries on where it left off."),
    ("I already have a list of new IDs.",
     "On step 4 press 'Fill from a spreadsheet'. It takes a CSV or Excel sheet with an old-ID column and a new-ID column; the old ID can be "
     "the Patient ID inside the scans or the name of each patient's folder.\n\nThe older list-driven methods are on step 1 under 'I already have a patient list'."),
    ("Does anything leave my computer?",
     "No. The app never connects to the internet."),
)


# ====================================================================== helpers

def open_path(p: Path) -> None:
    """Show a file or folder in Finder / Explorer. Local only; never a URL."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        elif os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except OSError as e:
        messagebox.showerror(APP_NAME, f"Could not open {p}\n{e}")


def open_with(target: Path, app_path: str = "") -> None:
    """Open a file or folder in the chosen viewer application (or the system default)."""
    cmd = model.external_viewer_command(target, app_path)
    try:
        if cmd is None:
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(cmd)
    except OSError as e:
        messagebox.showerror(APP_NAME, f"Could not open {target}\nwith {app_path or 'the default application'}\n{e}")


def short_path(p: str | Path, keep: int = 2) -> str:
    """'/a/very/long/way/to/Scans/2024' -> '.../Scans/2024'. The full path is in the tooltip and the summary."""
    parts = Path(p).parts
    return str(p) if len(parts) <= keep + 1 else ".../" + "/".join(parts[-keep:])


def shown_command(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


def confirm_typed(parent, title: str, message: str, word: str = "YES") -> bool:
    """A confirmation that needs the word typed, for actions that delete or release data."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.resizable(False, False)
    result = {"ok": False}
    f = ttk.Frame(win, padding=16)
    f.pack(fill="both", expand=True)
    ttk.Label(f, text=title, style="H2.TLabel").pack(anchor="w")
    ttk.Label(f, text=message, wraplength=480, justify="left").pack(anchor="w", pady=(8, 12))
    ttk.Label(f, text=f"Type {word} to continue:").pack(anchor="w")
    v = tk.StringVar()
    e = ttk.Entry(f, textvariable=v, width=16)
    e.pack(anchor="w", pady=(4, 12))
    b = ttk.Frame(f)
    b.pack(fill="x")
    ok = ttk.Button(b, text="Continue", style=theme.style_or("Accent.TButton"), command=lambda: (result.update(ok=True), win.destroy()))
    ok.pack(side="right")
    ok.state(["disabled"])
    ttk.Button(b, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 8))
    v.trace_add("write", lambda *_: ok.state(["!disabled"] if v.get().strip().upper() == word else ["disabled"]))
    e.focus_set()
    win.grab_set()
    parent.wait_window(win)
    return result["ok"]


class ConfirmRun(tk.Toplevel):
    """'Ready to anonymise?': one fact per row, room between them, two buttons. Replaces a system message box that
    ran five lines and two long folder paths together."""

    def __init__(self, parent, heading: str, rows: list[tuple[str, str, str]], note: str = "", ok_text: str = "Anonymise",
                 option: str = "", option_on: bool = True):
        super().__init__(parent)
        self.v_option = tk.BooleanVar(value=option_on and bool(option))
        self.title(APP_NAME)
        self.transient(parent)
        self.resizable(False, False)
        self.result = False
        f = ttk.Frame(self, padding=(28, 24, 28, 20))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=heading, font=("Helvetica Neue", 20, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))
        self.rows: list[tuple[str, str]] = []
        for i, (label, value, tip) in enumerate(rows, 1):
            ttk.Label(f, text=label, style="Muted.TLabel").grid(row=i, column=0, sticky="nw", padx=(0, 24), pady=7)
            v = ttk.Label(f, text=value, wraplength=400, justify="left", font=("Helvetica Neue", 14))
            v.grid(row=i, column=1, sticky="w", pady=7)
            if tip:
                Tooltip(v, tip)
            self.rows.append((label, value))
        n = len(rows) + 1
        if option:
            self.cb_option = ttk.Checkbutton(f, text=option, variable=self.v_option)
            self.cb_option.grid(row=n, column=0, columnspan=2, sticky="w", pady=(14, 0))
            n += 1
        if note:
            ttk.Label(f, text=note, style="Muted.TLabel", wraplength=540, justify="left").grid(row=n, column=0, columnspan=2, sticky="w", pady=(14, 0))
        b = ttk.Frame(f)
        b.grid(row=n + 1, column=0, columnspan=2, sticky="e", pady=(22, 0))
        self.b_ok = ttk.Button(b, text=ok_text, width=14, style=theme.style_or("Accent.TButton"), command=self._ok)
        self.b_ok.pack(side="right", ipady=5)
        self.b_cancel = ttk.Button(b, text="Not yet", width=10, command=self.destroy)
        self.b_cancel.pack(side="right", padx=(0, 10), ipady=5)
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()        # centre over the main window
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_reqwidth()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry(f"+{x}+{y}")
        self.b_ok.focus_set()

    def _ok(self) -> None:
        self.result = True
        self.destroy()

    def wait(self) -> bool:
        self.grab_set()
        self.master.wait_window(self)
        return self.result


class Tooltip:
    """A small hover balloon; the hints live here instead of on the page."""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 350):
        self.widget, self.text, self.delay = widget, text, delay_ms
        self._id: str | None = None
        self._win: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _schedule(self, _e=None) -> None:
        self._id = self.widget.after(self.delay, self._show)

    def _show(self) -> None:
        if self._win or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._win = tk.Toplevel(self.widget)
        self._win.wm_overrideredirect(True)
        self._win.wm_geometry(f"+{x}+{y}")
        pal = theme.palette()
        tk.Label(self._win, text=self.text, justify="left", wraplength=420, bg=pal["panel"], fg=pal["text"],
                 relief="solid", borderwidth=1, padx=10, pady=8, font=("TkDefaultFont", 11)).pack()

    def _hide(self, _e=None) -> None:
        if self._id:
            self.widget.after_cancel(self._id)
            self._id = None
        if self._win:
            self._win.destroy()
            self._win = None


class ChangeCard(ttk.Frame):
    """What anonymising does to one example file, in ordinary words: 'Name   SMITH JOHN   DEMO-001'."""

    def __init__(self, parent, columns: int = 1, wrap: int = 420, cut: tuple[int, int] = (22, 26), **kw):
        super().__init__(parent, padding=(14, 10, 14, 10), style=theme.style_or("Card.TFrame"), **kw)
        self.columns, self.wrap, self.cut = columns, wrap, cut
        self.v_title, self.v_more = tk.StringVar(), tk.StringVar()
        ttk.Label(self, textvariable=self.v_title, style="H2.TLabel").grid(row=0, column=0, sticky="w")
        self.body = ttk.Frame(self)
        self.body.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.lbl_more = ttk.Label(self, textvariable=self.v_more, style="Muted.TLabel", wraplength=wrap, justify="left")
        self.lbl_more.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.struck = tkfont.nametofont("TkDefaultFont").copy()
        self.struck.configure(overstrike=1)
        self.bold = tkfont.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold")

    @staticmethod
    def _cut(s: str, n: int) -> str:
        return s if len(s) <= n else s[:n - 1] + "\u2026"

    def show(self, title: str, changes: list, more: str) -> None:
        self.v_title.set(title)
        self.v_more.set(more)
        for w in self.body.winfo_children():
            w.destroy()
        per_col = -(-len(changes) // self.columns) if changes else 0
        pal = theme.palette()
        for i, c in enumerate(changes):
            col, row = (i // per_col) * 4, i % per_col
            ttk.Label(self.body, text=c.label, style="Muted.TLabel").grid(row=row, column=col, sticky="w", padx=((0 if col == 0 else 28), 10), pady=1)
            kept = c.state == "kept"
            ttk.Label(self.body, text=self._cut(c.before, self.cut[0]), font=("TkDefaultFont" if kept else self.struck)).grid(row=row, column=col + 1, sticky="w", padx=(0, 10))
            colour = {"removed": pal["muted"], "replaced": brand.TEAL_DARK, "kept": pal["warn"]}[c.state]
            ttk.Label(self.body, text=self._cut(c.after, self.cut[1]), foreground=colour, font=self.bold).grid(row=row, column=col + 2, sticky="w")

    def message(self, title: str, text: str) -> None:
        self.show(title, [], text)


def help_mark(parent, text: str) -> ttk.Label:
    q = ttk.Label(parent, text="?", style="Muted.TLabel", cursor="question_arrow")
    Tooltip(q, text)
    return q


# ====================================================================== the window

class App(tk.Tk):
    def __init__(self, selftest: bool = False, offer_demo: bool = True):
        super().__init__()
        self.selftest = selftest
        self.settings = Settings.load()
        if selftest:
            import tempfile
            self.settings = Settings(path=Path(tempfile.mkdtemp(prefix="scrubdicom_selftest_settings_")) / "settings.json")
        self.proc: EngineProcess | None = None
        self.job: str | None = None
        self.log_path: Path | None = None
        self.skipped_hidden = 0
        self.prog_state: dict = {}
        self.previewed_key: tuple | None = None
        self.demo_stage: str | None = None
        self.step = 0
        self._out_refresh_id: str | None = None
        self._validate_id: str | None = None
        self._viewers: list = []
        self.intake: intake.Intake | None = None      # what the opened folder holds
        self.kinds: list[intake.Kind] = []
        self.ticked: set[tuple] = set()               # kinds ticked under "Let me choose"
        self.overrides: dict[str, set[str]] = {}      # one study's own ticks from the viewer: {new ID: series UIDs}
        self._scan_thread: threading.Thread | None = None
        self._scan_cancel = False
        self._scan_prog = (0, 0)
        self._scan_result: intake.Intake | None = None
        self._ids_edited = False
        self._auto_dirs = ("", "")                    # the output / confidential folders this app last suggested
        self.kind_thumbs: dict[tuple, tk.PhotoImage] = {}
        self._thumb_queue: list = []
        self._card_key: tuple | None = None
        self._ids_version = 0                         # bumped whenever a new ID changes, so cached totals know to refresh
        self._memo: dict = {}
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.minsize(1040, 800)
        geo = self.settings.get("geometry")
        if not geo and self.winfo_screenheight() >= 900:
            geo = "1120x860"
        if geo:
            try:
                self.geometry(geo)
            except tk.TclError:
                pass
        self.theme_mode = theme.apply(self, self.settings.get("theme") or None)
        self._make_vars()
        self._make_menu()
        self._make_layout()
        self._apply_way()
        self._strip_changed()
        self._show_step(0)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = self._on_exception   # Tk callbacks: log (paths redacted) and tell the user
        self.v_output.trace_add("write", lambda *_: (self._schedule_output_refresh(), self._suggest_confidential()))
        self.v_manifest.trace_add("write", lambda *_: self._schedule_output_refresh())
        self.v_confidential.trace_add("write", lambda *_: self._assign_ids())
        self.v_output.trace_add("write", lambda *_: self._assign_ids())
        self.v_prefix.trace_add("write", lambda *_: self._assign_ids())
        self._refresh_all()
        self._watch_form()
        self.dnd_ok = dnd.enable(self, self._dropped)      # a folder dropped anywhere on the window opens it
        self._apply_way()
        self.after(POLL_MS, self._poll)
        if selftest:
            self.after(500, self._selftest_walk)

    # ================================================================== construction
    def _make_vars(self) -> None:
        s = self.settings
        sv = lambda k: tk.StringVar(value=str(s.get(k) or ""))
        bv = lambda k: tk.BooleanVar(value=bool(s.get(k)))
        self.v_mode = sv("mode")
        # paths are deliberately not remembered between launches: the app opens clean, and no folder names
        # (which are often hospital numbers) are ever written to the settings file
        self.v_output, self.v_manifest, self.v_remap, self.v_input = tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.v_confidential = tk.StringVar()
        self.v_series_select = tk.StringVar()      # written by the viewer when series are ticked
        self.v_study_id = tk.StringVar()
        self.v_mapping, self.v_current_col, self.v_new_col, self.v_sheet = tk.StringVar(), sv("current_col"), sv("new_col"), sv("sheet")
        self.v_match_on = tk.StringVar(value=s.get("match_on") or "patientid")
        self.v_match_on_label = tk.StringVar(value=MATCH_ON_LABELS.get(self.v_match_on.get(), MATCH_ON_LABELS["patientid"]))
        self.v_match_on_label.trace_add("write", lambda *_: self.v_match_on.set(MATCH_ON_KEYS.get(self.v_match_on_label.get(), "patientid")))
        self.v_series_pick = tk.StringVar()
        self.v_profile = sv("profile")
        self.v_profile_desc = tk.StringVar()
        self.v_profile_label = tk.StringVar()
        self.v_ctca, self.v_resume, self.v_keep_tech, self.v_flat = bv("ctca_only"), bv("resume"), bv("keep_technical"), bv("flat")
        self.v_verify_after, self.v_show_all = bv("verify_after_run"), bv("show_all_lines")
        self.v_more = tk.BooleanVar(value=self.v_mode.get() == "mapping")
        self.v_needles = tk.StringVar()              # never persisted: these are identifiers
        self.v_series_filter = tk.StringVar(value="All")
        self.v_series_query = tk.StringVar()
        self.v_progress = tk.DoubleVar(value=0.0)
        self.v_progress_text = tk.StringVar(value="")
        self.v_out_status = tk.StringVar(value="")
        self.v_drive_note = tk.StringVar(value="")
        self.v_status = tk.StringVar(value="")
        self.v_summary = tk.StringVar(value="")
        self.v_summary_short = tk.StringVar(value="")
        self.v_ready = tk.StringVar(value="")
        self.v_step = tk.StringVar(value="")
        self.v_strip = tk.StringVar(value="Not started")
        # the folder-first flow
        self.v_way = tk.StringVar(value="folder")            # folder | list
        self.v_listway = tk.BooleanVar(value=False)
        self.v_folder = tk.StringVar()
        self.v_folder_shown = tk.StringVar()
        self.v_cases = tk.StringVar(value="")                # "" until the user says: one | many
        self.v_folder_help = tk.StringVar()
        self.v_found = tk.StringVar(value="")
        self.v_found_notes = tk.StringVar(value="")
        self.v_choice = tk.StringVar(value="coronary" if self.v_ctca.get() else "all")
        self.v_plan = tk.StringVar(value="")
        self.v_kind_query = tk.StringVar(value="")           # the search over the kinds of series on step 2
        self.v_kind_shown = tk.StringVar(value="")
        self.v_strip_choice = tk.StringVar(value="saved" if self.v_profile.get().strip() else "default")
        self.v_keep = {k: tk.BooleanVar(value=False) for k, _, _ in KEEP_OPTIONS}
        self.v_prefix = tk.StringVar(value="ANON")
        self.v_one_id = tk.StringVar()
        self.v_nav_reason = tk.StringVar(value="")
        self.v_done_head = tk.StringVar(value="")
        self.v_done_detail = tk.StringVar(value="")
        self.v_show_log = tk.BooleanVar(value=False)
        self.v_banner = tk.StringVar(value="")

    def _make_menu(self) -> None:
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=False)
        f.add_command(label="Open a folder of scans...", command=self._home_open)
        f.add_command(label="Start again", command=self._reset)
        f.add_separator()
        f.add_command(label="Open output folder", command=lambda: self._open(self._out()))
        f.add_command(label="Open _logs folder", command=lambda: self._open(self._logs()))
        f.add_separator()
        f.add_command(label="Quit", command=self._on_close, accelerator="Cmd+Q" if sys.platform == "darwin" else "Alt+F4")
        m.add_cascade(label="File", menu=f)
        r = tk.Menu(m, tearoff=False)
        r.add_command(label="Preview (writes nothing)", command=lambda: self._start_run(dry_run=True))
        r.add_command(label="Anonymise", command=self._start_run)
        r.add_command(label="Stop", command=self._stop)
        r.add_command(label="Check the output", command=self._start_verify)
        r.add_command(label="Results", command=lambda: self._goto_step(DONE_STEP))
        r.add_command(label="Details: series decisions, check report, sharing checks", command=lambda: self._show_details("series"))
        r.add_separator()
        r.add_command(label="Viewer: preview original scans...", command=lambda: self._viewer("source"))
        r.add_command(label="Viewer: check anonymised output...", command=lambda: self._viewer("output"))
        r.add_separator()
        r.add_command(label="List patients whose kept slices are too thick", command=lambda: self._start_thick(fix=False))
        r.add_command(label="Remove those patients from the output so they are redone...", command=lambda: self._start_thick(fix=True))
        r.add_separator()
        r.add_command(label="See the demo (two made-up patients)", command=self._run_demo)
        r.add_command(label="Show the command this will run", command=self._show_command)
        m.add_cascade(label="Actions", menu=r)
        h = tk.Menu(m, tearoff=False)
        h.add_command(label="How it works", command=lambda: self.nb.select(self.tab_help))
        h.add_command(label="Full reference", command=self._reference)
        h.add_command(label="About", command=self._about)
        m.add_cascade(label="Help", menu=h)
        self.config(menu=m)

    def _make_layout(self) -> None:
        head = ttk.Frame(self, padding=(16, 10, 16, 8))
        head.pack(fill="x")
        brand.Wordmark(head, size=22, with_mark=48).pack(side="left")
        ttk.Label(head, text="Originals are never modified · no network", style="Muted.TLabel").pack(side="left", padx=(18, 0), pady=(10, 0))
        self.b_theme = ttk.Button(head, text="Dark" if self.theme_mode == "light" else "Light", width=6, command=self._toggle_theme)
        self.b_theme.pack(side="right")
        ttk.Label(head, textvariable=self.v_profile_label, style="Muted.TLabel").pack(side="right", padx=(0, 12), pady=(8, 0))
        ttk.Label(head, text="Profile:", style="Muted.TLabel").pack(side="right", padx=(0, 4), pady=(8, 0))
        brand.rule(self).pack(fill="x", padx=16)
        # the one status strip, on every tab
        self.strip = tk.Label(self, textvariable=self.v_strip, anchor="w", padx=16, pady=8, font=("Helvetica Neue", 13, "bold"))
        self.strip.pack(fill="x", padx=16, pady=(8, 0))
        self.banner = tk.Label(self, textvariable=self.v_banner, anchor="w", padx=16, pady=5, font=("TkDefaultFont", 12))
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(8, 0))
        self.tab_home, self.tab_run, self.tab_details, self.tab_help = (ttk.Frame(self.nb, padding=12) for _ in range(4))
        for tab, name in ((self.tab_home, "Home"), (self.tab_run, "Anonymise"), (self.tab_details, "Details"), (self.tab_help, "Help")):
            self.nb.add(tab, text=name)
        # The detail behind the results screen (series decisions, the check report, the sharing checks) is a tab that
        # stays hidden until asked for. Not a second window: on macOS, Tk redraws for ever when a window that has
        # never been shown is filled with data.
        self.nb.hide(self.tab_details)
        dtop = ttk.Frame(self.tab_details)
        dtop.pack(fill="x", pady=(0, 8))
        ttk.Button(dtop, text="\u2039 Back to the results", command=lambda: self._goto_step(DONE_STEP)).pack(side="left")
        self.nb_details = ttk.Notebook(self.tab_details)
        self.nb_details.pack(fill="both", expand=True)
        self.tab_series, self.tab_verify, self.tab_share = (ttk.Frame(self.nb_details, padding=12) for _ in range(3))
        for tab, name in ((self.tab_series, "Series decisions"), (self.tab_verify, "Check report"), (self.tab_share, "Sharing checks and logs")):
            self.nb_details.add(tab, text=name)
        self.brand_titles: list = []
        self._build_home_tab()
        self._build_run_tab()
        self._build_series_tab()
        self._build_verify_tab()
        self._build_share_tab()
        self._build_help_tab()
        bar = ttk.Frame(self, padding=(12, 4))
        bar.pack(fill="x")
        ttk.Label(bar, textvariable=self.v_status, style="Muted.TLabel").pack(side="left")
        ttk.Label(bar, text=f"Engine {model.ENGINE_VERSION} · {'packaged' if model.is_frozen() else 'source'}", style="Muted.TLabel").pack(side="right")
        self._apply_theme_colours()

    # ------------------------------------------------------------------ home
    def _build_home_tab(self) -> None:
        """A single centred column, as wide as the three action buttons, with a steady vertical rhythm."""
        t = self.tab_home
        COL = 840
        t.columnconfigure(0, weight=1)
        t.columnconfigure(2, weight=1)
        t.rowconfigure(0, weight=1)
        box = ttk.Frame(t, width=COL, padding=(0, 24, 0, 12))
        box.grid(row=0, column=1, sticky="ns")
        box.grid_propagate(False)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(3, weight=1)                 # the space between the card and the step strip absorbs extra height
        head = ttk.Frame(box)
        head.grid(row=0, column=0, sticky="ew")
        t0 = ttk.Label(head, text="Pseudonymise DICOM studies for blinded research reads.", font=("Helvetica Neue", 24, "bold"), foreground=brand.NAVY_DEEP, wraplength=COL)
        t0.pack(anchor="w")
        self.brand_titles.append(t0)
        ttk.Label(head, text="Built and validated on cardiac CT. Every output is checked before it is shared.", font=("Helvetica Neue", 14), style="Muted.TLabel", wraplength=COL).pack(anchor="w", pady=(6, 0))
        tiles = ttk.Frame(box)
        tiles.grid(row=1, column=0, sticky="ew", pady=(32, 0))
        tw = (COL - 32) // 3
        for i, (title, sub, colour, cmd) in enumerate((("Open scans", "Choose a folder of DICOM, or drop one here", brand.NAVY, self._home_open),
                                                       ("See the demo", "Two made-up patients, start to finish", brand.TEAL_DARK, self._run_demo),
                                                       ("Viewer", "Scans, headers, and what changes", brand.SLATE, lambda: self._viewer("source")))):
            brand.Tile(tiles, title, sub, colour, cmd, width=tw, height=124).grid(row=0, column=i, padx=(0 if i == 0 else 16, 0))
        self.v_home_recent = tk.StringVar(value="")
        recent = ttk.LabelFrame(box, text="This session", padding=(14, 8, 14, 12))
        recent.grid(row=2, column=0, sticky="ew", pady=(36, 0))
        ttk.Label(recent, textvariable=self.v_home_recent, wraplength=COL - 40, justify="left").pack(anchor="w")
        rb = ttk.Frame(recent)
        rb.pack(anchor="w", pady=(8, 0))
        self.b_home_continue = ttk.Button(rb, text="Continue", command=lambda: self._goto_step(SAVE_STEP))
        self.b_home_continue.pack(side="left")
        ttk.Button(rb, text="Results", command=lambda: self._goto_step(DONE_STEP)).pack(side="left", padx=(8, 0))
        ttk.Button(rb, text="Open output folder", command=lambda: self._open(self._out())).pack(side="left", padx=(8, 0))
        steps = ttk.Frame(box)
        steps.grid(row=4, column=0, sticky="ew", pady=(40, 0))
        for i in range(len(HELP_STEPS)):
            steps.columnconfigure(i, weight=1, uniform="step")
        for i, (title, text) in enumerate(HELP_STEPS):
            f = ttk.Frame(steps)
            f.grid(row=0, column=i, sticky="nw", padx=(0, 12))
            row = ttk.Frame(f)
            row.pack(anchor="w")
            num = tk.Canvas(row, width=28, height=28, highlightthickness=0, bg=brand._parent_bg(row))
            num.create_oval(1, 1, 27, 27, fill=brand.TEAL, outline=brand.TEAL)
            num.create_text(14, 14, text=str(i + 1), fill="white", font=("Helvetica Neue", 12, "bold"))
            num.pack(side="left", padx=(0, 8))
            ttk.Label(row, text=title, style="H2.TLabel").pack(side="left")
            ttk.Label(f, text=text, style="Muted.TLabel", wraplength=150, justify="left").pack(anchor="w", pady=(4, 0))

    def _refresh_home(self) -> None:
        out = self._out()
        s = self._spec()
        src = Path(s.manifest).name if s.mode == "manifest" and s.manifest else (Path(s.input).name if s.input else "")
        if self.v_way.get() == "folder":
            src = self.intake.root.name if self.intake else ""
        if not out:
            self.v_home_recent.set("Nothing open yet. Press Open scans, or see the demo.")
            self.b_home_continue.state(["disabled"])
            return
        done, partial = model.study_state(out) if out.is_dir() else ([], [])
        status, _, _ = model.verify_status(out / "_logs") if out.is_dir() else (None, None, "")
        line = f"Output: {out}"
        if src:
            line += f"   ·   scans: {src}"
        if self.v_confidential.get().strip():
            line += f"\nConfidential folder: {self.v_confidential.get().strip()}"
        line += f"\n{len(done)} patients done" + (f", {len(partial)} half-finished" if partial else "") + \
                ("   ·   checked: PASS" if status == "PASS" else "   ·   checked: FAIL" if status == "FAIL" else "   ·   not yet checked")
        self.v_home_recent.set(line)
        self.b_home_continue.state(["!disabled"])

    # ------------------------------------------------------------------ tab 1: the guided flow
    def _row(self, parent, r, label, var, kind=None, tip=None, filetypes=None, width=None):
        lbl = ttk.Label(parent, text=label)
        lbl.grid(row=r, column=0, sticky="w", padx=(0, 12), pady=6)
        e = ttk.Entry(parent, textvariable=var, width=width or 44)
        e.grid(row=r, column=1, sticky="ew", pady=6)
        widgets = [lbl, e]
        if kind:
            b = ttk.Button(parent, text="Choose...", command=lambda: self._browse(var, kind, filetypes))
            b.grid(row=r, column=2, padx=(8, 0), pady=6)
            widgets.append(b)
        if tip:
            q = help_mark(parent, tip)
            q.grid(row=r, column=3, padx=(8, 0))
            widgets.append(q)
        return widgets

    def _build_run_tab(self) -> None:
        t = self.tab_run
        t.columnconfigure(0, weight=1)
        t.rowconfigure(1, weight=1)
        toggle = theme.style_or("Toggle.TButton")
        switch = theme.style_or("Switch.TCheckbutton")
        accent = theme.style_or("Accent.TButton")
        csv_t = [("CSV files", "*.csv"), ("All files", "*")]

        stack = ttk.Frame(t)
        stack.grid(row=1, column=0, sticky="nsew")
        stack.columnconfigure(0, weight=1)
        stack.rowconfigure(0, weight=1)        # the steps share the spare height, so their tables grow with the window
        self.steps = [ttk.Frame(stack, padding=(8, 8, 8, 4)) for _ in STEP_TITLES]
        for i, s in enumerate(self.steps):
            s.grid(row=0, column=0, sticky="nsew")
            s.columnconfigure(1, weight=1)
            lbl = ttk.Label(s, text=STEP_TITLES[i], font=("Helvetica Neue", 22, "bold"), foreground=brand.NAVY_DEEP)
            lbl.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            self.brand_titles.append(lbl)
            self._step_nav(s, i)

        # ---- step 1: open a folder
        s1 = self.steps[0]
        q = ttk.Frame(s1)
        q.grid(row=1, column=0, columnspan=5, sticky="w", pady=(0, 12))
        ttk.Label(q, text="What are you anonymising?", style="H2.TLabel").pack(anchor="w", pady=(0, 8))
        qt = ttk.Frame(q)
        qt.pack(anchor="w")
        self.case_tiles = {}
        for key, text in (("one", "One patient\na single case"), ("many", "Several patients\na folder of cases")):
            rb = ttk.Radiobutton(qt, text=text, value=key, variable=self.v_cases, command=self._apply_way, style=toggle, width=24)
            rb.pack(side="left", padx=(0, 10), ipady=14)
            self.case_tiles[key] = rb
        ff = self.f_folder = ttk.Frame(s1)
        ff.grid(row=2, column=0, columnspan=5, sticky="nsew")
        ff.columnconfigure(1, weight=1)
        ff.rowconfigure(4, weight=1)
        ttk.Label(ff, textvariable=self.v_folder_help, style="Muted.TLabel", wraplength=WRAP, justify="left").grid(row=0, column=0, columnspan=3, sticky="w")
        self.b_open = ttk.Button(ff, text="Choose folder...", style=accent, width=28, command=self._open_folder)
        self.b_open.grid(row=1, column=0, sticky="w", pady=(10, 8), ipady=6)
        self.lbl_folder = ttk.Label(ff, textvariable=self.v_folder_shown, style="Muted.TLabel")
        self.lbl_folder.grid(row=1, column=1, sticky="w", padx=(12, 0))
        self._folder_tip = Tooltip(self.lbl_folder, "")
        self.b_scan_stop = ttk.Button(ff, text="Stop reading", command=self._cancel_scan)
        self.b_scan_stop.grid(row=1, column=2, sticky="e")
        self.b_scan_stop.grid_remove()
        fr = ttk.Frame(ff)
        fr.grid(row=2, column=0, columnspan=3, sticky="w")
        self.lbl_found = ttk.Label(fr, textvariable=self.v_found, style="Big.TLabel", wraplength=WRAP - 260, justify="left")
        self.lbl_found.pack(side="left")
        self.b_switch = ttk.Button(fr, text="Switch to several patients", command=self._switch_to_many)
        ttk.Label(ff, textvariable=self.v_found_notes, style="Muted.TLabel", wraplength=WRAP, justify="left").grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 6))
        fc = ttk.Frame(ff)
        fc.grid(row=4, column=0, columnspan=3, sticky="nsew")
        fc.columnconfigure(0, weight=1)
        fc.rowconfigure(0, weight=1)
        self.tv_found = self._tree(fc, [("folder", "Folder", 300), ("pid", "Patient ID in the scans", 170), ("series", "Series", 60),
                                        ("images", "Images", 70), ("cor", "Coronary CT", 260)], height=4, xscroll=False)

        fl = self.f_list = ttk.Frame(s1)
        fl.grid(row=3, column=0, columnspan=5, sticky="nsew")
        fl.columnconfigure(1, weight=1)
        tiles = ttk.Frame(fl)
        tiles.grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self.tiles = {}
        # a single patient needs no list: that is what "Choose folder" does, so only the two list-driven methods are offered here
        texts = {"manifest": "A list of patients\nCSV: folder, new ID", "mapping": "Folder of patients\n+ ID spreadsheet"}
        for mode in LIST_MODES:
            rb = ttk.Radiobutton(tiles, text=texts[mode], value=mode, variable=self.v_mode, command=self._apply_mode, style=toggle, width=22)
            rb.pack(side="left", padx=(0, 10), ipady=14)
            self.tiles[mode] = rb
        self.lbl_mode = ttk.Label(fl, text="", style="Muted.TLabel", wraplength=860, justify="left")
        self.lbl_mode.grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.rows_manifest = self._row(fl, 3, "Patient list", self.v_manifest, "file", "A CSV with two columns: source_folder (the folder holding that patient's scans) and study_id (the new ID). One patient per row.", csv_t)
        self.rows_input = self._row(fl, 4, "Scans folder", self.v_input, "dir", "All sub-folders are searched.")
        self.rows_study_id = self._row(fl, 5, "New ID", self.v_study_id, None, "Applied to every file in the folder. Letters, digits, spaces, - _ . (spaces become _ in folder names).", width=24)
        self.rows_mapping = self._row(fl, 6, "ID spreadsheet", self.v_mapping, "file", "CSV or Excel with an old-ID column and a new-ID column. Column names are auto-detected; see the columns row if not.",
                                      [("Spreadsheets", "*.csv *.xlsx *.xlsm"), ("All files", "*")])
        adv = ttk.Frame(fl)
        adv.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        adv.columnconfigure(1, weight=1)
        self.adv1 = adv
        self.rows_remap = self._row(adv, 0, "Drive path fix", self.v_remap, None, "Only when the patient list was written on another computer: OLD=NEW prefix, e.g. /Volumes/Drive=E:\\", width=30)
        self.rows_picks = self._row(adv, 1, "Already-analysed series", self.v_series_pick, "file", "CSV (study_id, series_uid, series_description) naming the series that must be kept for a patient.", csv_t)
        cols = ttk.Frame(adv)
        cols.grid(row=2, column=1, columnspan=3, sticky="w", pady=6)
        lbl = ttk.Label(adv, text="Spreadsheet columns")
        lbl.grid(row=2, column=0, sticky="w", padx=(0, 12), pady=6)
        self.rows_cols = [lbl, cols]
        for i, (text, var, width) in enumerate((("old ID", self.v_current_col, 12), ("new ID", self.v_new_col, 12), ("sheet", self.v_sheet, 8))):
            ttk.Label(cols, text=text).grid(row=0, column=2 * i, sticky="w", padx=(0 if i == 0 else 12, 4))
            ttk.Entry(cols, textvariable=var, width=width).grid(row=0, column=2 * i + 1, sticky="w")
        ttk.Label(cols, text="the old ID is the").grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Combobox(cols, textvariable=self.v_match_on_label, values=tuple(MATCH_ON_LABELS.values()), state="readonly", width=22).grid(row=1, column=2, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Checkbutton(fl, text="Rarely needed options", variable=self.v_more, command=self._apply_mode, style=toggle).grid(row=8, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.cb_listway = ttk.Checkbutton(s1, text="I already have a patient list (CSV) or an ID spreadsheet", variable=self.v_listway, command=self._apply_way)
        self.cb_listway.grid(row=4, column=0, columnspan=4, sticky="w", pady=(12, 0))

        # ---- step 2: which scans
        s2 = self.steps[1]
        rbf = ttk.Frame(s2)
        rbf.grid(row=1, column=0, columnspan=5, sticky="w")
        self.rb_choice = {}
        for i, (key, title, sub) in enumerate(SERIES_CHOICES):
            r = ttk.Radiobutton(rbf, text=title, value=key, variable=self.v_choice, command=self._choice_changed)
            r.grid(row=i, column=0, sticky="w", pady=3)
            ttk.Label(rbf, text=sub, style="Muted.TLabel").grid(row=i, column=1, sticky="w", padx=(12, 0))
            self.rb_choice[key] = r
        fk = self.f_kinds = ttk.Frame(s2)
        fk.grid(row=2, column=0, columnspan=5, sticky="nsew", pady=(8, 0))
        fk.columnconfigure(0, weight=1)
        # a cohort from several hospitals names the same series many ways: find them by a word, tick what is shown
        find = ttk.Frame(fk)
        find.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(find, text="Find").pack(side="left")
        e_find = ttk.Entry(find, textvariable=self.v_kind_query, width=26)
        e_find.pack(side="left", padx=(8, 0))
        e_find.bind("<Escape>", lambda _e: self.v_kind_query.set(""))
        Tooltip(e_find, "Type part of a series name, a type or a slice thickness: 'cta', 'pulm', 'ct 1 mm', 'calcium'. Every word must match.")
        self.b_find_clear = ttk.Button(find, text="Show all", command=lambda: self.v_kind_query.set(""))
        self.b_find_clear.pack(side="left", padx=(8, 0))
        ttk.Label(find, textvariable=self.v_kind_shown, style="Muted.TLabel").pack(side="left", padx=(12, 0))
        self.v_kind_query.trace_add("write", lambda *_: self._fill_kinds())
        kc = ttk.Frame(fk)
        kc.grid(row=1, column=0, sticky="nsew")
        kc.columnconfigure(0, weight=1)
        kc.rowconfigure(0, weight=1)
        self.tv_kinds = self._tree(kc, [("use", "Keep", 50), ("mod", "Type", 55), ("desc", "Series", 270), ("slice", "Slice", 70), ("in", "Found in", 100),
                                        ("images", "Images", 70), ("note", "What it is", 270)], height=5, xscroll=False)
        # a picture of each kind in the tree column: a clinician recognises the coronary series by eye
        ttk.Style(self).configure("Kinds.Treeview", rowheight=THUMB + 6)
        self.tv_kinds.configure(show="tree headings", style="Kinds.Treeview")
        self.tv_kinds.column("#0", width=THUMB + 22, minwidth=THUMB + 22, stretch=False, anchor="center")
        self.tv_kinds.heading("#0", text="")
        self.tv_kinds.bind("<ButtonRelease-1>", self._kind_click)
        fk.rowconfigure(1, weight=1)
        s2.rowconfigure(2, weight=1)
        krow = ttk.Frame(fk)
        krow.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(krow, textvariable=self.v_plan, style="H2.TLabel").pack(side="left")
        self.b_look = ttk.Button(krow, text="Look at the images...", command=lambda: self._viewer("source"))
        self.b_look.pack(side="right")
        self.b_tick_none = ttk.Button(krow, text="Untick shown", command=lambda: self._tick_all(False))
        self.b_tick_none.pack(side="right", padx=(0, 8))
        self.b_tick_all = ttk.Button(krow, text="Tick shown", command=lambda: self._tick_all(True))
        self.b_tick_all.pack(side="right", padx=(0, 8))
        self.lbl_list_series = ttk.Label(s2, text="With a patient list the choice applies to every patient. To tick series for one patient, open the viewer on the last step.",
                                         style="Muted.TLabel", wraplength=WRAP, justify="left")
        self.lbl_list_series.grid(row=3, column=0, columnspan=5, sticky="w", pady=(8, 0))

        # ---- step 3: what to remove
        s3 = self.steps[2]
        body3 = ttk.Frame(s3)
        body3.grid(row=1, column=0, columnspan=5, sticky="nsew")
        body3.columnconfigure(1, weight=1)
        f3 = ttk.Frame(body3)
        f3.grid(row=0, column=0, sticky="nw")
        self.card_remove = ChangeCard(body3, columns=1, wrap=360, cut=(16, 19))
        self.card_remove.grid(row=0, column=1, sticky="ne", padx=(20, 0))
        ttk.Radiobutton(f3, text="Everything that identifies the patient, the hospital or the scanner", value="default", variable=self.v_strip_choice,
                        command=self._strip_changed).grid(row=0, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(f3, text="Recommended. This is what a blinded read needs.", style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", padx=(28, 0))
        ttk.Radiobutton(f3, text="Everything, except what I tick here", value="custom", variable=self.v_strip_choice,
                        command=self._strip_changed).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 2))
        kf = ttk.Frame(f3)
        kf.grid(row=3, column=0, columnspan=3, sticky="w", padx=(28, 0))
        self.cb_keep = []
        for i, (key, title, sub) in enumerate(KEEP_OPTIONS):
            cb = ttk.Checkbutton(kf, text=title, variable=self.v_keep[key], command=self._strip_changed)
            cb.grid(row=i, column=0, sticky="w", pady=1)
            ttk.Label(kf, text=sub, style="Muted.TLabel").grid(row=i, column=1, sticky="w", padx=(14, 0))
            self.cb_keep.append(cb)
        ttk.Radiobutton(f3, text="Use a saved profile", value="saved", variable=self.v_strip_choice,
                        command=self._strip_changed).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 2))
        prow = ttk.Frame(f3)
        prow.grid(row=5, column=0, columnspan=3, sticky="w", padx=(28, 0))
        self.cb_profile = ttk.Combobox(prow, textvariable=self.v_profile_label, state="readonly", width=34)
        self.cb_profile.pack(side="left")
        self.cb_profile.bind("<<ComboboxSelected>>", lambda _e: self._profile_chosen())
        self.b_edit_profiles = ttk.Button(prow, text="Edit...", command=self._edit_profiles)
        self.b_edit_profiles.pack(side="left", padx=(8, 0))
        # what is always removed is not repeated here: the card beside these options shows it on one of the user's own files

        # ---- step 4: new IDs, where to save, go
        s4 = self.steps[3]
        fi = self.f_ids = ttk.Frame(s4)
        fi.grid(row=1, column=0, columnspan=5, sticky="nsew")
        fi.columnconfigure(0, weight=1)
        one = self.f_one = ttk.Frame(fi)
        one.grid(row=0, column=0, sticky="w")
        ttk.Label(one, text="New ID for this patient").pack(side="left")
        e1 = ttk.Entry(one, textvariable=self.v_one_id, width=24)
        e1.pack(side="left", padx=(12, 0))
        self.v_one_id.trace_add("write", lambda *_: self._one_id_typed())
        help_mark(one, "The name the anonymised copy is filed under and the only identity inside its files. Letters, digits, spaces, - _ .").pack(side="left", padx=(8, 0))
        many = self.f_many = ttk.Frame(fi)
        many.grid(row=1, column=0, sticky="nsew")
        many.columnconfigure(0, weight=1)
        top = ttk.Frame(many)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="New IDs start with").pack(side="left")
        ttk.Entry(top, textvariable=self.v_prefix, width=12).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Renumber", command=lambda: self._assign_ids(force=True)).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Fill from a spreadsheet...", command=self._ids_from_sheet).pack(side="left", padx=(8, 0))
        ttk.Label(top, text="Double-click an ID to change it.", style="Muted.TLabel").pack(side="left", padx=(12, 0))
        ic = ttk.Frame(many)
        ic.grid(row=1, column=0, sticky="nsew", pady=(6, 2))
        ic.columnconfigure(0, weight=1)
        ic.rowconfigure(0, weight=1)
        self.tv_ids = self._tree(ic, [("folder", "Original", 380), ("new_id", "New ID", 200), ("keeps", "Keeps", 280)], height=3, xscroll=False)
        self.tv_ids.bind("<Double-1>", self._edit_id)
        fp = ttk.Frame(s4)
        fp.grid(row=2, column=0, columnspan=5, sticky="ew")
        fp.columnconfigure(1, weight=1)
        self._row(fp, 0, "Anonymised copies go to", self.v_output, "dir", "One folder per new ID is created here. Only anonymised files ever go here, so this folder can be handed over.")
        ttk.Button(fp, text="Open", width=8, command=lambda: self._open(self._out())).grid(row=0, column=4, padx=(8, 0))
        ttk.Label(fp, textvariable=self.v_out_status, style="Muted.TLabel").grid(row=1, column=1, columnspan=4, sticky="w")
        self._row(fp, 2, "The confidential key goes to", self.v_confidential, "dir", "The list linking each new ID to the real patient, and the logs. Must be outside the output folder. Keep it; never send it with the scans.")
        ttk.Button(fp, text="Suggest", width=8, command=lambda: self.v_confidential.set(model.suggest_confidential(self.v_output.get()))).grid(row=2, column=4, padx=(8, 0))
        self.lbl_drive = ttk.Label(s4, textvariable=self.v_drive_note, style="Warn.TLabel", wraplength=WRAP, justify="left")
        self.lbl_drive.grid(row=5, column=0, columnspan=5, sticky="w")
        sw = ttk.Frame(s4)
        sw.grid(row=6, column=0, columnspan=5, sticky="w", pady=(4, 0))
        ttk.Checkbutton(sw, text="Check the output when done", variable=self.v_verify_after, style=switch).pack(side="left", padx=(0, 24))
        self.cb_resume = ttk.Checkbutton(sw, text="Skip patients already done", variable=self.v_resume, style=switch)
        self.cb_resume.pack(side="left", padx=(0, 24))
        self.lbl_manifest_only = ttk.Label(sw, text="", style="Muted.TLabel")
        self.lbl_manifest_only.pack(side="left")
        adv3 = ttk.Frame(sw)
        adv3.pack(side="left")
        self.adv3 = adv3
        ttk.Checkbutton(adv3, text="Keep scanner technical details", variable=self.v_keep_tech, style=switch).pack(side="left", padx=(0, 24))
        ttk.Checkbutton(adv3, text="No series sub-folders", variable=self.v_flat, style=switch).pack(side="left")
        self.lbl_summary = ttk.Label(s4, textvariable=self.v_summary_short, wraplength=WRAP, justify="left", style="H2.TLabel")
        self.lbl_summary.grid(row=7, column=0, columnspan=5, sticky="w", pady=(6, 4))
        act = ttk.Frame(s4)
        act.grid(row=8, column=0, columnspan=5, sticky="w", pady=(2, 0))
        self.b_start = ttk.Button(act, text="Anonymise", width=16, style=accent, command=self._start_run)
        self.b_dry = ttk.Button(act, text="Preview", width=12, command=lambda: self._start_run(dry_run=True))
        self.b_stop = ttk.Button(act, text="Stop", width=8, command=self._stop, state="disabled")
        self.b_start.pack(side="left", ipady=6)
        self.b_dry.pack(side="left", padx=(10, 0), ipady=6)
        self.b_stop.pack(side="left", padx=(10, 0), ipady=6)
        self.b_viewer = ttk.Button(act, text="Open viewer", command=lambda: self._viewer("source"))
        self.b_viewer.pack(side="left", padx=(28, 0), ipady=6)
        Tooltip(self.b_dry, "A dry run: reads everything and reports what would be written, without writing anything.")
        self.lbl_ready = ttk.Label(s4, textvariable=self.v_ready, wraplength=WRAP, justify="left")
        self.lbl_ready.grid(row=9, column=0, columnspan=5, sticky="w", pady=(4, 0))

        # ---- step 5: done. One headline, the things to do next, what still needs a human, and what was removed
        s5 = self.steps[4]
        hero = ttk.Frame(s5)
        hero.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(4, 0))
        self.cv_done = tk.Canvas(hero, width=60, height=60, highlightthickness=0, bg=brand._parent_bg(hero))
        self.cv_done.pack(side="left")
        ht = ttk.Frame(hero)
        ht.pack(side="left", padx=(16, 0))
        self.lbl_done_head = ttk.Label(ht, textvariable=self.v_done_head, font=("Helvetica Neue", 20, "bold"), wraplength=WRAP - 100, justify="left")
        self.lbl_done_head.pack(anchor="w")
        ttk.Label(ht, textvariable=self.v_done_detail, style="Muted.TLabel", wraplength=WRAP - 100, justify="left").pack(anchor="w", pady=(2, 0))
        dact = ttk.Frame(s5)
        dact.grid(row=2, column=0, columnspan=5, sticky="w", pady=(12, 0))
        self.b_done_view = ttk.Button(dact, text="View the scans", style=accent, command=lambda: self._viewer("output", self._first_output_id()))
        self.b_done_open = ttk.Button(dact, text="Open the folder", command=lambda: self._open(self._out()))
        self.b_done_hand = ttk.Button(dact, text="Hand over", command=self._handover)
        self.b_done_cert = ttk.Button(dact, text="Certificate (PDF)", command=self._open_certificate)
        self.b_done_check = ttk.Button(dact, text="Check again", command=self._start_verify)
        self.b_done_details = ttk.Button(dact, text="Details...", command=lambda: self._show_details("series"))
        for i, b in enumerate((self.b_done_view, self.b_done_open, self.b_done_hand, self.b_done_cert, self.b_done_check, self.b_done_details)):
            b.pack(side="left", padx=(0 if i == 0 else 8, 0), ipady=6)
        Tooltip(self.b_done_hand, "Compares every file with the checksum list written at the check, then opens the folder. Only an unchanged output is handed over.")
        Tooltip(self.b_done_cert, "A one-page record of what was anonymised and checked. It carries no patient identifiers and travels with the output.")
        Tooltip(self.b_done_details, "Every series decision, the full check report (and extra words to search for), and the sharing checks.")
        self.f_issues = ttk.Frame(s5)
        self.f_issues.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        self.card_done = ChangeCard(s5, columns=2, wrap=WRAP - 60)
        self.card_done.grid(row=4, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Button(s5, text="Open a different output folder...", command=self._choose_output).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # ---- one slim row under the steps: what Next is waiting for, and the rarely used actions
        nav = ttk.Frame(t, padding=(8, 2))
        nav.grid(row=2, column=0, sticky="ew")
        ttk.Label(nav, textvariable=self.v_nav_reason, style="Warn.TLabel", wraplength=700, justify="left").pack(side="left")
        ttk.Checkbutton(nav, text="Activity log", variable=self.v_show_log, command=self._toggle_log, style=toggle).pack(side="right", padx=(8, 0))
        self.b_reset = ttk.Button(nav, text="Start again", command=self._reset)
        Tooltip(self.b_reset, "Clear the folder, the choices and the new IDs, and go back to the first step. Nothing on disk is deleted or changed.")
        more = ttk.Menubutton(nav, text="More")
        mm = tk.Menu(more, tearoff=False)
        mm.add_command(label="Show the command this will run", command=self._show_command)
        mm.add_command(label="Open output folder", command=lambda: self._open(self._out()))
        mm.add_command(label="Open log file", command=lambda: self._open(self.log_path))
        mm.add_command(label="Copy log", command=self._copy_log)
        mm.add_command(label="See the demo (two made-up patients)", command=self._run_demo)
        mm.add_separator()
        mm.add_checkbutton(label="Show patients skipped as already done", variable=self.v_show_all)
        mm.add_checkbutton(label="Show the rarely needed options", variable=self.v_more, command=self._apply_mode)
        more["menu"] = mm
        more.pack(side="right")
        self.b_reset.pack(side="right", padx=(0, 8))

        # ---- recovery card (hidden until needed)
        self.card = ttk.Frame(t, padding=(16, 10, 16, 12), style=theme.style_or("Card.TFrame"))
        self.card.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.card.columnconfigure(0, weight=1)
        self.v_card_title, self.v_card_text = tk.StringVar(), tk.StringVar()
        ttk.Label(self.card, textvariable=self.v_card_title, style="H2.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self.card, textvariable=self.v_card_text, wraplength=620, justify="left").grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.b_card = ttk.Button(self.card, text="", style=accent)
        self.b_card.grid(row=0, column=1, rowspan=2, padx=(16, 0))
        self.b_card2 = ttk.Button(self.card, text="")
        self.b_card2.grid(row=0, column=2, rowspan=2, padx=(8, 0))
        ttk.Button(self.card, text="Dismiss", command=self._hide_card).grid(row=0, column=3, rowspan=2, padx=(8, 0))
        self.card.grid_remove()

        # ---- progress and activity
        prog = self.f_prog = ttk.Frame(t, padding=(8, 0))        # appears with the first job
        prog.grid(row=3, column=0, sticky="ew", pady=(6, 4))
        prog.columnconfigure(0, weight=1)
        prog.grid_remove()
        self.bar = ttk.Progressbar(prog, variable=self.v_progress, maximum=100)
        self.bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(prog, textvariable=self.v_progress_text, style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
        logf = self.f_log = ttk.Frame(t, padding=(8, 0))        # the engine's own words: hidden until asked for
        logf.grid(row=4, column=0, sticky="nsew")
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        logf.grid_remove()
        self.log = tk.Text(logf, font=MONO, wrap="none", state="disabled", height=3, width=60, undo=False)
        ys = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ys.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        self._profile_entries = []
        self._refresh_profile_label()

    def _step_nav(self, frame, index: int) -> None:
        """The step pills and Back / Next beside the title, so navigation is always in view."""
        nav = ttk.Frame(frame)
        nav.grid(row=0, column=3, columnspan=2, sticky="e", pady=(0, 8))
        brand.StepPills(nav, len(STEP_TITLES), index, STEP_SHORT).pack(side="left", padx=(0, 14))
        b_back = ttk.Button(nav, text="\u2039 Back", width=8, command=lambda: self._show_step(index - 1))
        b_back.pack(side="left")
        if index == 0:
            b_back.state(["disabled"])
        if index < DONE_STEP:
            b_next = ttk.Button(nav, text="Results \u203a" if index == SAVE_STEP else "Next \u203a", width=9,
                                style=theme.style_or("Accent.TButton") if index < SAVE_STEP else "TButton", command=lambda: self._show_step(index + 1))
            b_next.pack(side="left", padx=(6, 0))
            self._top_next = getattr(self, "_top_next", {})
            self._top_next[index] = b_next

    def _apply_way(self) -> None:
        """Folder-first (the default) or the list-driven methods: show the matching half of each step."""
        cases = self.v_cases.get()
        if self.v_listway.get() and cases != "many":
            if cases == "one":
                self.v_listway.set(False)             # a list only makes sense for several patients
            else:
                cases = "many"                        # asked for the list methods without answering: that is several patients
                self.v_cases.set(cases)
        listway = self.v_listway.get()
        self.v_way.set("list" if listway else "folder")
        # nothing but the question until it has been answered, unless a folder was dropped and is being read
        show_folder = (bool(cases) or self._scanning() or self.intake is not None) and not listway
        (self.f_list.grid if listway else self.f_list.grid_remove)()
        (self.f_folder.grid if show_folder else self.f_folder.grid_remove)()
        (self.cb_listway.grid if cases == "many" else self.cb_listway.grid_remove)()
        self.steps[0].rowconfigure(2, weight=1 if show_folder else 0)
        one = cases == "one"
        drop = " Or drop the folder on this window." if getattr(self, "dnd_ok", False) else ""
        self.v_folder_help.set(("Choose this patient's folder. Every sub-folder is searched. Nothing in it is changed." if one else
                                "Choose the folder that holds all the patients, each in a folder of their own. Every sub-folder is searched. Nothing in it is changed.") + drop)
        self.b_open.configure(text="Choose the patient's folder..." if one else "Choose the folder of patients...")
        self._refresh_found_headline()
        (self.f_kinds.grid_remove if listway else self.f_kinds.grid)()
        (self.lbl_list_series.grid if listway else self.lbl_list_series.grid_remove)()
        (self.f_ids.grid_remove if listway else self.f_ids.grid)()
        self._apply_mode()

    def _apply_mode(self) -> None:
        mode = self.v_mode.get()
        more = self.v_more.get()
        listway = self.v_way.get() == "list"
        if mode not in LIST_MODES:      # an older setting, or the engine's single-folder mode: not offered in this window
            mode = "manifest"
            self.v_mode.set(mode)
        # the spreadsheet method always shows how the old ID is matched; the patient list keeps its extras behind the toggle
        groups = {"manifest": self.rows_manifest, "mapping": self.rows_input + self.rows_mapping + self.rows_cols}
        adv_groups = {"manifest": self.rows_remap + self.rows_picks, "mapping": []}
        every = set(sum(groups.values(), []) + sum(adv_groups.values(), [])) | set(self.rows_study_id)
        show = set(groups.get(mode, [])) | (set(adv_groups.get(mode, [])) if more else set())
        for w in every:
            (w.grid if w in show else w.grid_remove)()
        (self.adv1.grid if mode == "mapping" or (more and adv_groups.get(mode)) else self.adv1.grid_remove)()
        (self.adv3.pack if more else self.adv3.pack_forget)(**({"side": "left"} if more else {}))
        manifest = mode == "manifest" or not listway
        self.cb_resume.state(["!disabled"] if manifest and not (self.intake and self.intake.mixed and not listway) else ["disabled"])
        self.lbl_manifest_only.configure(text="" if manifest else "(patient-list runs only)   ")
        self.lbl_mode.configure(text=model.MODE_HELP.get(mode, ""))
        # the series choices that make sense here: a list of patients can be 'coronary only' or everything; ticking kinds needs an opened folder
        self.rb_choice["coronary"].state(["!disabled"] if manifest else ["disabled"])
        self.rb_choice["choose"].state(["disabled"] if listway else ["!disabled"])
        if (listway and self.v_choice.get() == "choose") or (not manifest and self.v_choice.get() == "coronary"):
            self.v_choice.set("all")
        self.v_ctca.set(self.v_choice.get() == "coronary")
        self._schedule_validate()

    def _show_step(self, i: int) -> None:
        self.step = max(0, min(DONE_STEP, i))
        for k, s in enumerate(self.steps):
            if k == self.step:
                s.tkraise()
            else:
                s.lower()
        self.v_step.set(STEP_TITLES[self.step])
        if self.step == 1:
            self._fill_kinds()
        elif self.step == 2:
            self._refresh_change_cards()
        elif self.step == SAVE_STEP:
            self._fill_ids()
        elif self.step == DONE_STEP:
            self._refresh_done()
        self._live_validate()

    def _toggle_log(self) -> None:
        """The engine's line-by-line output, for the curious and for bug reports. Off by default."""
        if self.v_show_log.get():
            self.f_log.grid()
            self.tab_run.rowconfigure(4, weight=1)
            self.log.see("end")
            # a small window has no room to spare for it: make room rather than squeeze the steps
            self.update_idletasks()
            h, room = self.winfo_height(), self.winfo_screenheight() - 90
            if h < 900 and room > h:
                self.geometry(f"{self.winfo_width()}x{min(room, h + 110)}")
        else:
            self.f_log.grid_remove()
            self.tab_run.rowconfigure(4, weight=0)

    # ------------------------------------------------------------------ the results screen
    def _example(self):
        """(file, new ID, label) of one series the run keeps, to show what anonymising does to it; None before a folder is open."""
        it = self.intake
        if not it or not it.units or self.v_way.get() != "folder":
            return None
        for u in it.units:
            kept = intake.kept_series(u, self.v_choice.get(), self.ticked, self.overrides.get(u.new_id))
            for s in kept or u.series:
                if s.sample is not None:
                    return s.sample, u.new_id.strip() or "NEW-ID", it.label(u)
        return None

    def _refresh_change_cards(self) -> None:
        """Fill the 'what this does' card on step 3 and the 'what was removed' card on the results screen."""
        if not hasattr(self, "card_done"):
            return
        ex = self._example()
        key = (ex, self.v_profile.get(), tuple(v.get() for v in self.v_keep.values()), self.v_strip_choice.get(), self.v_keep_tech.get(), theme.current())
        if key == self._card_key:
            return
        self._card_key = key
        if ex is None:
            self.card_remove.message("On one of your scans", "Open a folder on step 1 and this shows what happens to one of its files.")
            self.card_done.grid_remove()
            return
        path, new_id, label = ex
        try:
            rows = pv.header_diff(path, new_id, keep_technical=self.v_keep_tech.get(), profile=model.profile_for_path(self.v_profile.get()))
            changes, more = plain.plain_changes(rows)
        except Exception:
            self.card_remove.message("On one of your scans", "That file's header could not be read for the example.")
            self.card_done.grid_remove()
            return
        self.card_remove.show("What this does, on one of your scans", changes, more)
        self.card_done.show(f"What was removed, shown on {new_id}", changes, more)
        self.card_done.grid()

    def _first_output_id(self) -> str | None:
        """The output folder name of this run's first study, so the viewer opens on what was just anonymised."""
        if self.v_way.get() == "folder" and self.intake and self.intake.units:
            return intake.safe_name(self.intake.units[0].new_id)
        return None

    def _refresh_done(self) -> None:
        if not hasattr(self, "cv_done"):
            return
        st = model.done_state(self._out())
        self.v_done_head.set(st.headline)
        self.v_done_detail.set(st.detail)
        pal = theme.palette()
        colour = {"ok": brand.TEAL_DARK, "warn": brand.AMBER, "error": brand.RED, "none": pal["border"]}[st.level]
        cv = self.cv_done
        cv.configure(bg=brand._parent_bg(cv.master))
        cv.delete("all")
        cv.create_oval(3, 3, 57, 57, fill=colour, outline=colour)
        if st.level == "ok":
            cv.create_line(17, 31, 26, 40, 43, 21, fill="white", width=5, capstyle="round", joinstyle="round")
        elif st.level == "error":
            cv.create_line(20, 20, 40, 40, fill="white", width=5, capstyle="round")
            cv.create_line(40, 20, 20, 40, fill="white", width=5, capstyle="round")
        elif st.level == "warn":
            cv.create_line(30, 16, 30, 34, fill="#1f2328", width=5, capstyle="round")
            cv.create_oval(27, 40, 33, 46, fill="#1f2328", outline="#1f2328")
        else:
            cv.create_line(20, 30, 40, 30, fill=pal["muted"], width=5, capstyle="round")
        running = bool(self.proc and self.proc.running)
        something = st.level != "none"
        self.b_done_view.state(["!disabled"] if something else ["disabled"])
        self.b_done_open.state(["!disabled"] if something else ["disabled"])
        self.b_done_hand.state(["!disabled"] if st.can_hand_over and not running else ["disabled"])
        self.b_done_cert.state(["!disabled"] if st.verified else ["disabled"])
        self.b_done_check.state(["!disabled"] if something and not running else ["disabled"])
        self.b_done_check.configure(text="Check again" if st.verified else "Check now")
        self.b_done_details.state(["!disabled"] if something else ["disabled"])
        for w in self.f_issues.winfo_children():
            w.destroy()
        actions = {"review": ("Look at them", lambda: self._viewer("output")), "move_logs": ("Move them out...", self._move_logs),
                   "verify": ("Check now", self._start_verify), "report": ("Open the report", lambda: self._show_details("verify"))}
        issues = list(st.issues)
        out = self._out()
        if self.v_way.get() == "folder" and self.intake and out and out.is_dir() and st.level != "none":
            mine = {intake.safe_name(u.new_id) for u in self.intake.units}
            done, partial = model.study_state(out)
            others = sorted(x for x in done + partial if x not in mine)
            if others:
                issues.append(model.Issue(f"This folder also holds {len(others)} from earlier runs ({', '.join(others[:4])}{', ...' if len(others) > 4 else ''}). "
                                          "The counts above include them.", ""))
        for i, issue in enumerate(issues):
            row = ttk.Frame(self.f_issues)
            row.grid(row=i, column=0, sticky="w", pady=2)
            mark = tk.Canvas(row, width=18, height=18, highlightthickness=0, bg=brand._parent_bg(row))
            mark.create_oval(1, 1, 17, 17, fill=brand.AMBER, outline=brand.AMBER)
            mark.create_text(9, 9, text="!", fill="#1f2328", font=("Helvetica Neue", 11, "bold"))
            mark.pack(side="left", padx=(0, 8))
            ttk.Label(row, text=issue.text, wraplength=WRAP - 260, justify="left").pack(side="left")
            if issue.action in actions:
                text, cmd = actions[issue.action]
                ttk.Button(row, text=text, command=cmd).pack(side="left", padx=(12, 0))
        self._refresh_change_cards()

    def _show_details(self, which: str = "series") -> None:
        self._refresh_all()
        self.nb.add(self.tab_details)        # un-hide; it then stays in the tab bar
        self.nb.select(self.tab_details)
        self.nb_details.select({"series": self.tab_series, "verify": self.tab_verify, "share": self.tab_share}[which])

    def _choose_output(self) -> None:
        """Look at the results of an earlier run: point the app at its output folder."""
        p = filedialog.askdirectory(title="Choose an output folder that was anonymised earlier", mustexist=True,
                                    initialdir=str(self._out().parent if self._out() and self._out().parent.is_dir() else Path.home()))
        if p:
            self.v_output.set(p)
            self._refresh_all()

    def _open_certificate(self) -> None:
        out = self._out()
        try:
            path = certificate.write_certificate(out) if out else None
        except OSError as e:
            messagebox.showerror(APP_NAME, f"The certificate could not be written:\n{e}")
            return
        if path is None:
            messagebox.showinfo(APP_NAME, "There is no certificate yet: the output has to pass the check first.")
            return
        self._refresh_all()
        self._open(path)

    def _notify(self, message: str) -> None:
        """Tell someone who has walked away that a long job has finished: a desktop notification where the system has
        one built in, the bell and the window to the front everywhere."""
        if self.selftest:
            return
        try:
            self.bell()
            cmd = model.notify_command(APP_NAME, message)
            if cmd:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.deiconify()
                self.lift()
        except (OSError, tk.TclError):
            pass

    # ------------------------------------------------------------------ the folder-first flow
    def _reset(self, ask: bool = True) -> bool:
        """Start again: forget the folder, the choices, the new IDs and the destinations, and return to the first step
        with nothing answered. Only the form is cleared: nothing on disk is deleted, moved or changed."""
        if self.proc and self.proc.running:
            messagebox.showinfo(APP_NAME, "A job is running. Stop it first, then start again.")
            return False
        if self._scanning():
            self._scan_cancel = True
            self.v_status.set("Stopping the read of the folder. Press Start again once more when it has stopped.")
            return False
        if ask and (self._ids_edited or self.overrides) and not messagebox.askokcancel(
                "Start again?", "The new IDs you typed and the series you ticked will be cleared.\n\nNothing on disk is deleted or changed."):
            return False
        self.demo_stage = None
        self.previewed_key = None
        self.intake, self.kinds, self.ticked, self.overrides = None, [], set(), {}
        self.kind_thumbs, self._thumb_queue, self._card_key, self._memo = {}, [], None, {}
        self._ids_edited, self._ids_version, self._auto_dirs = False, self._ids_version + 1, ("", "")
        self._last_suggested = ""
        for v in (self.v_folder, self.v_folder_shown, self.v_found, self.v_found_notes, self.v_cases, self.v_kind_query, self.v_one_id,
                  self.v_output, self.v_confidential, self.v_manifest, self.v_input, self.v_study_id, self.v_mapping, self.v_remap,
                  self.v_series_pick, self.v_series_select, self.v_needles, self.v_progress_text, self.v_plan):
            v.set("")
        self._folder_tip.text = ""
        self.v_listway.set(False)
        self.v_prefix.set("ANON")
        self.v_choice.set("coronary")
        self.v_strip_choice.set("default")
        for v in self.v_keep.values():
            v.set(False)
        self.v_progress.set(0)
        self._hide_card()
        self._clear_log()
        self.f_prog.grid_remove()
        self._strip_changed()
        self._apply_way()
        self._fill_found()
        self._fill_kinds()
        self._fill_ids()
        self._goto_step(0)
        self._refresh_all()
        self.v_status.set("Started again. Nothing on disk was changed.")
        return True

    def _home_open(self) -> None:
        self.v_listway.set(False)
        self._apply_way()
        self._goto_step(0)

    def _case_mismatch(self) -> str:
        """Said 'one patient' but the folder holds several: almost always the wrong folder, so say so and stop."""
        it = self.intake
        if self.v_cases.get() == "one" and it and it.n_patients > 1 and self.v_way.get() == "folder":
            return f"This folder holds {it.n_patients} patients, not one. Choose that one patient's folder, or switch to several patients."
        return ""

    def _refresh_found_headline(self) -> None:
        it = self.intake
        if self._scanning() or not hasattr(self, "b_switch"):
            return
        bad = self._case_mismatch()
        if it is None:
            self.v_found.set("")
        else:
            self.v_found.set(bad or (it.problems[0] if it.problems else it.headline()))
        self.lbl_found.configure(style="Warn.TLabel" if bad else "Big.TLabel")
        if bad:
            self.b_switch.pack(side="left", padx=(12, 0))
        else:
            self.b_switch.pack_forget()

    def _switch_to_many(self) -> None:
        self.v_cases.set("many")
        self._apply_way()
        self._fill_ids_if_showing()
        self._live_validate()

    def _scanning(self) -> bool:
        return self._scan_thread is not None and self._scan_thread.is_alive()

    def _open_folder(self, path: str | None = None, wait: bool = False) -> None:
        """Choose a folder and read what is in it, on a worker thread so a slow drive never freezes the window."""
        if self._busy():
            return
        if self._scanning():
            return
        if not path:
            path = filedialog.askdirectory(initialdir=str(self.intake.root.parent if self.intake else Path.home()), mustexist=True,
                                           title="Choose the folder that holds the scans")
        if not path:
            return
        root = Path(path)
        self.v_folder.set(str(root))
        self.v_folder_shown.set(short_path(root))
        self._folder_tip.text = str(root)
        self.intake, self.kinds, self.ticked, self.overrides = None, [], set(), {}
        self._ids_edited = False
        self._scan_cancel, self._scan_prog, self._scan_result = False, (0, 0), None
        self.v_found.set("Reading the folder...")
        self.v_found_notes.set("")
        self._fill_found()
        self.b_open.state(["disabled"])
        self.b_scan_stop.grid()

        def work() -> None:
            try:
                res = intake.scan_folder(root, progress=lambda n, s: setattr(self, "_scan_prog", (n, s)), cancelled=lambda: self._scan_cancel)
            except Exception as e:   # a worker thread must never die silently
                res = intake.Intake(root=root, problems=[f"The folder could not be read: {type(e).__name__}: {model.redact_paths(str(e))[:160]}"])
            self._scan_result = res

        gc.collect()      # finalise closed windows' Tk variables here, on the main thread, not from the worker's collector
        self._scan_thread = threading.Thread(target=work, daemon=True)
        self._scan_thread.start()
        self._apply_way()
        self._live_validate()
        if wait:      # the demo and the self-test: stay here until the folder has been read and the tables are filled
            while self._scan_thread is not None:
                self._poll_scan()
                self.update()
                time.sleep(0.02)

    def _dropped(self, folder: Path) -> None:
        """A folder was dropped on the window: the same as choosing it. Whether it is one patient or several is read
        from what the folder holds, unless the question has already been answered."""
        if (self.proc and self.proc.running) or self._scanning():
            self.v_status.set("Busy: drop the folder again when this has finished.")
            return
        self.v_listway.set(False)
        self._goto_step(0)
        self._open_folder(str(folder))

    def _cancel_scan(self) -> None:
        self._scan_cancel = True

    def _poll_scan(self) -> None:
        if self._scan_thread is None:
            return
        if self._scan_result is None:
            n, s = self._scan_prog
            self.v_found.set(f"Reading the folder...  {n:,} files, {s} series so far" if n else "Reading the folder...")
            return
        it, self._scan_result, self._scan_thread = self._scan_result, None, None
        self.b_open.state(["!disabled"])
        self.b_scan_stop.grid_remove()
        self._scan_done(it)

    def _scan_done(self, it: intake.Intake) -> None:
        self.intake = it
        self.kinds = intake.series_kinds(it)
        self.ticked = intake.default_ticks(self.kinds)
        self.overrides = {}
        notes = list(it.notes)
        if it.cancelled:
            notes.insert(0, "Reading was stopped early: only what was read so far is listed.")
        if it.units and not self.v_cases.get():       # opened without answering the question (the demo, the tests): the folder answers it
            self.v_cases.set("one" if it.n_patients == 1 else "many")
        if it.units:
            none = sum(1 for u in it.units if not any(s.verdict == "keep" for s in u.series))
            if none and none < len(it.units):
                notes.append(f"{none} of {len(it.units)} have no clear coronary CT series; see the last column.")
            # where the copies could go: suggested once per opened folder, never over a place the user picked
            out_now, conf_now = self.v_output.get().strip(), self.v_confidential.get().strip()
            if not out_now or (out_now, conf_now) == self._auto_dirs:
                out, conf = intake.suggest_folders(it.root)
                self._auto_dirs = (out, conf)
                self.v_output.set(out)
                self.v_confidential.set(conf)
            self.v_choice.set("coronary" if any(k.coronary for k in self.kinds) and not self.rb_choice["coronary"].instate(["disabled"]) else "all")
        self.v_found_notes.set("  ".join(notes))
        self.kind_thumbs = {}
        self.v_kind_query.set("")
        self._thumb_queue = [k for k in self.kinds if k.sample is not None]
        self._card_key = None
        self.after(30, self._thumb_tick)
        self._apply_way()
        self._assign_ids(force=True)
        self._fill_found()
        self._fill_kinds()
        self._fill_ids_if_showing()
        self._refresh_all()
        self._live_validate()

    def _thumb_tick(self) -> None:
        """One thumbnail per tick, so a cohort with fifty kinds of series never freezes the window."""
        if not self._thumb_queue or not pv.viewer_available()[0]:
            self._thumb_queue = []
            return
        k = self._thumb_queue.pop(0)
        try:
            img = pv.thumbnail(k.sample, THUMB)
            if img is not None:
                photo = tk.PhotoImage(data=pv.to_ppm(img))
                self.kind_thumbs[k.key] = photo
                if k in self.kinds and self.tv_kinds.exists(str(self.kinds.index(k))):
                    self.tv_kinds.item(str(self.kinds.index(k)), image=photo)
        except Exception:       # a picture is a nicety: an unreadable file must never stop the flow
            pass
        if self._thumb_queue:
            self.after(5, self._thumb_tick)

    def _state_sig(self) -> tuple:
        """A cheap fingerprint of everything the totals depend on: the folder, the choice, the ticks, the new IDs."""
        return (id(self.intake), self.v_choice.get(), hash(frozenset(self.ticked)),
                hash(frozenset((k, frozenset(v)) for k, v in self.overrides.items())), self._ids_version)

    def _memoised(self, name: str, fn):
        """Totals over a cohort are asked for several times per click; with thousands of studies, work them out once."""
        sig = self._state_sig()
        if self._memo.get("sig") != sig:
            self._memo = {"sig": sig}
        if name not in self._memo:
            self._memo[name] = fn()
        return self._memo[name]

    def _plan(self) -> intake.Plan:
        if not self.intake:
            return intake.Plan(0, 0, 0, [])
        return self._memoised("plan", lambda: intake.plan(self.intake, self.v_choice.get(), self.ticked, self.overrides))

    def _selection_rows(self) -> list:
        return self._memoised("rows", lambda: intake.selection_rows(self.intake, self.v_choice.get(), self.ticked, self.overrides))

    def _fill_ids_if_showing(self) -> None:
        """The table of new IDs has a row per study; rebuild it only while it is on screen (it is filled on arrival)."""
        if self.step == SAVE_STEP:
            self._fill_ids()

    def _fill_found(self) -> None:
        tv = self.tv_found
        tv.delete(*tv.get_children(""))
        it = self.intake
        if not it:
            return
        for i, u in enumerate(it.units):
            keep = [s for s in u.series if s.verdict == "keep"]
            maybe = [s for s in u.series if s.verdict == "maybe"]
            cor = (f"{len(keep)} series, {sum(s.n_images for s in keep):,} images" if keep
                   else ("possibly: a thin CT without contrast noted" if maybe else "none found"))
            tv.insert("", "end", iid=str(i), values=(it.label(u), u.key or "(none)", len(u.series), f"{u.n_images:,}", cor), tags=(() if keep else ("warn",)))
        theme.tag_colours(tv, ("warn",))

    def _fill_kinds(self) -> None:
        tv = self.tv_kinds
        tv.delete(*tv.get_children(""))
        it = self.intake
        choice = self.v_choice.get()
        choosing = choice == "choose"
        for b in (self.b_tick_all, self.b_tick_none):
            b.state(["!disabled"] if choosing and it else ["disabled"])
        self.b_look.state(["!disabled"] if it and it.units else ["disabled"])
        if not it:
            self.v_plan.set("" if self.v_way.get() == "list" else "Open a folder on step 1 to see its series here.")
            return
        n = len(it.units)
        query = self.v_kind_query.get().strip()
        shown = 0
        for i, k in enumerate(self.kinds):
            if query and not k.matches(query):
                continue
            shown += 1
            if choosing:
                kept = k.key in self.ticked
                mark = "\u2611" if kept else "\u2610"
            else:
                kept = choice == "all" or k.coronary > 0 or (k.maybe > 0 and not any(x.coronary for x in self.kinds))
                mark = "\u2713" if kept else ""
            tv.insert("", "end", iid=str(i), image=self.kind_thumbs.get(k.key, ""), values=(mark, k.modality, k.description or "(no description)", k.slice_text,
                                                                                          f"{k.n_units} of {n}", f"{k.n_images:,}", k.note), tags=(("keep",) if kept else ("drop",)))
        theme.tag_colours(tv, ("keep", "drop"))
        total = len(self.kinds)
        self.v_kind_shown.set((f"{shown} of {total} kinds shown" if query else f"{total} kinds of series") if total else "")
        self.b_find_clear.state(["!disabled"] if query else ["disabled"])
        text = self._plan().text(n, it.noun(n))
        if self.overrides:
            text += f"  {len(self.overrides)} patient(s) have their own ticks from the viewer."
        self.v_plan.set(text)

    def _kind_click(self, e) -> None:
        if self.v_choice.get() != "choose":
            if self.tv_kinds.identify_row(e.y) and self.tv_kinds.identify_column(e.x) == "#1":
                self.v_status.set("Choose 'Let me choose' to tick series by hand.")
            return
        row = self.tv_kinds.identify_row(e.y)
        if not row or self.tv_kinds.identify_region(e.x, e.y) not in ("cell", "tree"):
            return
        key = self.kinds[int(row)].key
        (self.ticked.discard if key in self.ticked else self.ticked.add)(key)
        self._fill_kinds()
        self._fill_ids_if_showing()
        self._live_validate()

    def _shown_kinds(self) -> list:
        query = self.v_kind_query.get().strip()
        return [k for k in self.kinds if not query or k.matches(query)]

    def _tick_all(self, on: bool) -> None:
        """Tick or untick every kind that is showing: all of them, or just what the search found."""
        keys = {k.key for k in self._shown_kinds()}
        self.ticked = (self.ticked | keys) if on else (self.ticked - keys)
        self._fill_kinds()
        self._fill_ids_if_showing()
        self._live_validate()

    def _choice_changed(self) -> None:
        self.v_ctca.set(self.v_choice.get() == "coronary")
        self._fill_kinds()
        self._fill_ids_if_showing()
        self._live_validate()

    def _strip_changed(self) -> None:
        """Step 3: turn the choice into the profile the engine reads. Ticks are saved as an ordinary profile file."""
        c = self.v_strip_choice.get()
        for cb in self.cb_keep:
            cb.state(["!disabled"] if c == "custom" else ["disabled"])
        self.cb_profile.configure(state="readonly" if c == "saved" else "disabled")
        self.b_edit_profiles.state(["!disabled"] if c == "saved" else ["disabled"])
        if c == "default":
            self.v_profile.set("")
        elif c == "custom":
            p = Profile(name="Chosen in the app", **{k: v.get() for k, v in self.v_keep.items()})
            if p.is_default():
                self.v_profile.set("")
            else:
                try:
                    self.v_profile.set(str(p.save(model.profiles_dir() / CUSTOM_PROFILE)))
                except OSError as e:
                    messagebox.showerror(APP_NAME, f"Could not save the choice: {e}")
        self._refresh_profile_label()
        if c == "saved":
            self._profile_chosen()
        self._refresh_change_cards()
        self._schedule_validate()

    def _assign_ids(self, force: bool = False) -> None:
        it = self.intake
        if not it or not it.units or (self._ids_edited and not force):
            return
        self._ids_edited = False
        conf, out = self.v_confidential.get().strip(), self._out()
        taken: set[str] = set()
        if out and out.is_dir():
            done, partial = model.study_state(out)
            taken = set(done) | set(partial)
        # an earlier session's ID is kept only where that patient has already been written to this output: that is
        # when renumbering would anonymise someone twice. A list that was never run does not pin anything.
        existing = {k: v for k, v in (intake.read_patient_list(conf, it.mixed) if conf else {}).items() if intake.safe_name(v) in taken}
        intake.assign_ids(it, self.v_prefix.get(), existing, taken)
        self._ids_version += 1
        self._fill_ids_if_showing()
        self._schedule_validate()

    def _fill_ids(self) -> None:
        tv = self.tv_ids
        tv.delete(*tv.get_children(""))
        it = self.intake
        single = bool(it and len(it.units) == 1)
        (self.f_one.grid if single else self.f_one.grid_remove)()
        (self.f_many.grid_remove if single else self.f_many.grid)()
        if not it:
            return
        if single and self.v_one_id.get() != it.units[0].new_id:
            self.v_one_id.set(it.units[0].new_id)
        choice = self.v_choice.get()
        for i, u in enumerate(it.units):
            kept = intake.kept_series(u, choice, self.ticked, self.overrides.get(u.new_id))
            keeps = f"{len(kept)} series, {sum(s.n_images for s in kept):,} images" if kept else "nothing: skipped"
            if u.new_id in self.overrides:
                keeps += "  (ticked in the viewer)"
            tv.insert("", "end", iid=str(i), values=(it.label(u), u.new_id, keeps), tags=(() if kept else ("warn",)))
        theme.tag_colours(tv, ("warn",))

    def _one_id_typed(self) -> None:
        it = self.intake
        if it and len(it.units) == 1 and it.units[0].new_id != self.v_one_id.get().strip():
            self._rename(it.units[0], self.v_one_id.get().strip())

    def _rename(self, unit: intake.Unit, new_id: str) -> None:
        if unit.new_id in self.overrides:
            self.overrides[new_id] = self.overrides.pop(unit.new_id)
        unit.new_id = new_id
        self._ids_version += 1
        self._ids_edited = True
        self._schedule_validate()

    def _edit_id(self, e) -> None:
        tv = self.tv_ids
        row = tv.identify_row(e.y)
        if not row or not self.intake:
            return
        box = tv.bbox(row, "new_id")
        if not box:
            return
        unit = self.intake.units[int(row)]
        var = tk.StringVar(value=unit.new_id)
        ent = ttk.Entry(tv, textvariable=var)
        ent.place(x=box[0], y=box[1], width=box[2], height=box[3])
        ent.focus_set()
        ent.select_range(0, "end")

        def commit(_e=None) -> None:
            if ent.winfo_exists():
                value = var.get().strip()
                ent.destroy()
                if value and value != unit.new_id:
                    self._rename(unit, value)
                    self._fill_ids()

        ent.bind("<Return>", commit)
        ent.bind("<FocusOut>", commit)
        ent.bind("<Escape>", lambda _e: ent.destroy())

    def _ids_from_sheet(self) -> None:
        it = self.intake
        if not it or not it.units:
            return
        p = filedialog.askopenfilename(title="Choose the spreadsheet of old and new IDs", filetypes=[("Spreadsheets", "*.csv *.xlsx *.xlsm"), ("All files", "*")])
        if not p:
            return
        try:
            from scrubdicom.core import load_mapping
            mapping = load_mapping(p, None, None, None)
        except SystemExit as e:
            messagebox.showerror(APP_NAME, f"That spreadsheet could not be used.\n\n{e}")
            return
        except Exception as e:
            messagebox.showerror(APP_NAME, f"That spreadsheet could not be read: {type(e).__name__}")
            return
        n = intake.apply_spreadsheet(it, mapping)
        self._ids_version += 1
        self._ids_edited = True
        self._fill_ids()
        self._live_validate()
        messagebox.showinfo(APP_NAME, f"{n} of {len(it.units)} matched a row in the spreadsheet." +
                            ("" if n == len(it.units) else "\n\nThe others keep the ID they had. The old ID is looked up as the Patient ID inside the scans, then as the folder name."))

    def _materialise(self) -> bool:
        """Folder way: write the patient list and the series selection into the confidential folder, where the engine
        reads them. Called just before a run, a preview or the viewer needs them; nothing is written earlier."""
        it = self.intake
        conf = self.v_confidential.get().strip()
        if not it or not it.units:
            return False
        if not conf:
            messagebox.showinfo(APP_NAME, "Choose where the confidential key goes first (step 4).")
            self._goto_step(SAVE_STEP)
            return False
        try:
            intake.run_list(conf, it)
            intake.write_patient_list(conf, it)
            intake.write_selection(conf, self._selection_rows())
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Could not write to the confidential folder:\n{e}")
            return False
        return True

    def ticks_cleared(self, study_id: str) -> None:
        """Called by the viewer after 'Let the rule decide': that study follows the choice on step 2 again."""
        if self.overrides.pop(study_id, None) is not None:
            self._fill_kinds()
            self._fill_ids_if_showing()
            self._live_validate()

    # ------------------------------------------------------------------ tabs 2-4 with empty states
    def _tree(self, container, columns: list[tuple[str, str, int]], height=12, xscroll: bool = True) -> ttk.Treeview:
        frame = ttk.Frame(container)
        frame.grid(row=0, column=0, sticky="nsew")
        tv = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height, selectmode="browse")
        for key, head, width in columns:
            tv.heading(key, text=head, command=lambda k=key, t=tv: self._sort_tree(t, k, False))
            tv.column(key, width=width, minwidth=40, stretch=(width >= 200), anchor="w")
        ys = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=tv.xview)
        tv.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tv.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        if xscroll:
            xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tv.frame = frame  # type: ignore[attr-defined]
        return tv

    def _empty(self, container, text: str, button: str | None, command=None) -> ttk.Frame:
        f = ttk.Frame(container, padding=40)
        f.grid(row=0, column=0, sticky="nsew")
        ttk.Label(f, text=text, style="Big.TLabel", wraplength=700, justify="center").pack(pady=(30, 12))
        if button:
            ttk.Button(f, text=button, style=theme.style_or("Accent.TButton"), command=command).pack(ipady=6)
        return f

    @staticmethod
    def _toggle_empty(tv: ttk.Treeview, empty: ttk.Frame, is_empty: bool) -> None:
        if is_empty:
            tv.frame.grid_remove()  # type: ignore[attr-defined]
            empty.grid()
        else:
            empty.grid_remove()
            tv.frame.grid()  # type: ignore[attr-defined]

    @staticmethod
    def _container(parent) -> ttk.Frame:
        c = ttk.Frame(parent)
        c.pack(fill="both", expand=True)
        c.rowconfigure(0, weight=1)
        c.columnconfigure(0, weight=1)
        return c

    @staticmethod
    def _sort_tree(tv: ttk.Treeview, key: str, reverse: bool) -> None:
        items = [(tv.set(i, key), i) for i in tv.get_children("")]
        try:
            items.sort(key=lambda x: float(x[0]), reverse=reverse)
        except ValueError:
            items.sort(key=lambda x: x[0].lower(), reverse=reverse)
        for n, (_, i) in enumerate(items):
            tv.move(i, "", n)
        tv.heading(key, command=lambda: App._sort_tree(tv, key, not reverse))

    def _build_series_tab(self) -> None:
        t = self.tab_series
        top = ttk.Frame(t)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Show").pack(side="left")
        for f in model.SERIES_FILTERS:
            ttk.Radiobutton(top, text=f, value=f, variable=self.v_series_filter, command=self._refresh_series).pack(side="left", padx=(8, 0))
        ttk.Label(top, text="New ID contains").pack(side="left", padx=(24, 6))
        e = ttk.Entry(top, textvariable=self.v_series_query, width=18)
        e.pack(side="left")
        e.bind("<KeyRelease>", lambda _e: self._refresh_series())
        ttk.Button(top, text="Export CSV...", command=self._export_series).pack(side="right")
        ttk.Button(top, text="Refresh", command=self._refresh_series).pack(side="right", padx=(0, 8))
        ttk.Button(top, text="Open in viewer", command=self._viewer_selected_series).pack(side="right", padx=(0, 8))
        c = self._container(t)
        self.tv_series = self._tree(c, [("study_id", "New ID", 100), ("series_number", "Series", 55), ("original_description", "Original series name", 220),
                                        ("images", "Images", 60), ("decision", "Decision", 70), ("reason", "Why", 300), ("run", "Run", 110)])
        self.empty_series = self._empty(c, "No series decisions yet.\nThey appear here after a run that keeps only some series.", None)
        self.lbl_series = ttk.Label(t, text="", style="Muted.TLabel")
        self.lbl_series.pack(anchor="w", pady=(6, 0))
        self.series_rows: list[dict] = []

    def _build_verify_tab(self) -> None:
        t = self.tab_verify
        top = ttk.Frame(t)
        top.pack(fill="x")
        ttk.Label(top, text="Words that must never appear in the output (surnames, hospital numbers), comma-separated").pack(anchor="w")
        row = ttk.Frame(top)
        row.pack(fill="x", pady=(4, 8))
        ttk.Entry(row, textvariable=self.v_needles).pack(side="left", fill="x", expand=True)
        self.b_verify = ttk.Button(row, text="Check now", style=theme.style_or("Accent.TButton"), command=self._start_verify)
        self.b_verify.pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Open report", command=lambda: self._open(model.verify_status(self._logs())[1] if self._logs() else None)).pack(side="left", padx=(8, 0))
        self.lbl_verify = ttk.Label(t, text="Not checked yet.", style="Big.TLabel")
        self.lbl_verify.pack(anchor="w", pady=(0, 6))
        c = self._container(t)
        vf = ttk.Frame(c)
        vf.grid(row=0, column=0, sticky="nsew")
        vf.rowconfigure(0, weight=1)
        vf.rowconfigure(2, weight=1)
        vf.columnconfigure(0, weight=1)
        self.txt_verify = tk.Text(vf, font=MONO, wrap="none", height=10, state="disabled")
        self.txt_verify.grid(row=0, column=0, sticky="nsew")
        ttk.Label(vf, text="Per-patient summary of the latest run").grid(row=1, column=0, sticky="w", pady=(10, 4))
        sc = ttk.Frame(vf)
        sc.grid(row=2, column=0, sticky="nsew")
        sc.rowconfigure(0, weight=1)
        sc.columnconfigure(0, weight=1)
        self.tv_summary = self._tree(sc, [("study_id", "New ID", 100), ("status", "Status", 110), ("files", "Files", 55), ("series", "Series", 55),
                                         ("review_files", "To _review", 75), ("errors", "Errors", 55), ("source_folder", "Original folder (confidential)", 300)], height=8)
        self.verify_frame = vf
        self.empty_verify = self._empty(c, "Nothing to check yet.\nAnonymise some patients first; the check runs by itself when the run finishes.", None)

    def _build_share_tab(self) -> None:
        t = self.tab_share
        ttk.Label(t, text="Is the output folder safe to hand over?", style="H2.TLabel").pack(anchor="w")
        ttk.Label(t, text="The whole _logs folder is confidential. Move it out before the output leaves this computer.", style="Muted.TLabel").pack(anchor="w", pady=(2, 6))
        c = self._container(t)
        self.tv_checks = self._tree(c, [("mark", "", 28), ("title", "Check", 300), ("detail", "Detail", 500)], height=7)
        self.empty_share = self._empty(c, "Nothing to share yet.\nAnonymise some patients first.", None)
        row = ttk.Frame(t)
        row.pack(fill="x", pady=8)
        self.b_handover = ttk.Button(row, text="Hand over: re-check and open the output folder", style=theme.style_or("Accent.TButton"), command=self._handover)
        self.b_handover.pack(side="left", ipady=4)
        self.b_share_check = ttk.Button(row, text="Check now", command=self._start_verify)
        self.b_share_check.pack(side="left", padx=(8, 0))
        self.b_move_logs = ttk.Button(row, text="Move confidential logs out...", command=self._move_logs)
        self.b_move_logs.pack(side="left", padx=(8, 0))
        self.b_review = ttk.Button(row, text="Review quarantined files...", command=lambda: self._viewer("output"))
        self.b_review.pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Refresh", command=self._refresh_share).pack(side="right")
        self.lbl_handover = ttk.Label(t, text="", style="Warn.TLabel", wraplength=900, justify="left")
        self.lbl_handover.pack(anchor="w")
        ttk.Label(t, text="Files in the logs folder (_logs)").pack(anchor="w", pady=(10, 4))
        c2 = self._container(t)
        self.tv_logs = self._tree(c2, [("name", "File", 300), ("size", "Size", 70), ("modified", "Modified", 130), ("note", "", 320)], height=7)

    def _build_help_tab(self) -> None:
        """Four steps and a handful of questions. The long reference is one click away, not the first thing seen."""
        t = self.tab_help
        t.columnconfigure(0, weight=1)
        t.rowconfigure(3, weight=1)
        head = ttk.Frame(t)
        head.grid(row=0, column=0, sticky="ew", pady=(4, 0))
        h = ttk.Label(head, text="How it works", font=("Helvetica Neue", 22, "bold"), foreground=brand.NAVY_DEEP)
        h.pack(side="left")
        self.brand_titles.append(h)
        ttk.Button(head, text="Full reference", command=self._reference).pack(side="right")
        ttk.Button(head, text="See the demo", style=theme.style_or("Accent.TButton"), command=self._run_demo).pack(side="right", padx=(0, 8))
        steps = ttk.Frame(t)
        steps.grid(row=1, column=0, sticky="ew", pady=(18, 0))
        for i, (title, text) in enumerate(HELP_STEPS):
            steps.columnconfigure(i, weight=1, uniform="hs")
            f = ttk.Frame(steps)
            f.grid(row=0, column=i, sticky="nw", padx=(0, 16))
            row = ttk.Frame(f)
            row.pack(anchor="w")
            num = tk.Canvas(row, width=28, height=28, highlightthickness=0, bg=brand._parent_bg(row))
            num.create_oval(1, 1, 27, 27, fill=brand.TEAL, outline=brand.TEAL)
            num.create_text(14, 14, text=str(i + 1), fill="white", font=("Helvetica Neue", 12, "bold"))
            num.pack(side="left", padx=(0, 8))
            ttk.Label(row, text=title, style="H2.TLabel").pack(side="left")
            ttk.Label(f, text=text, wraplength=165, justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Label(t, text="Common questions", style="H2.TLabel").grid(row=2, column=0, sticky="w", pady=(28, 8))
        qa = ttk.Frame(t)
        qa.grid(row=3, column=0, sticky="nsew")
        qa.columnconfigure(1, weight=1)
        qa.rowconfigure(0, weight=1)
        self.v_help_idx = tk.IntVar(value=0)
        ql = ttk.Frame(qa)
        ql.grid(row=0, column=0, sticky="nw")
        for i, (q, _a) in enumerate(HELP_QA):
            ttk.Radiobutton(ql, text=q, value=i, variable=self.v_help_idx, command=self._help_answer,
                            style=theme.style_or("Toggle.TButton"), width=38).pack(anchor="w", pady=2, ipady=2)
        ans = ttk.Frame(qa, padding=(24, 0, 0, 0))
        ans.grid(row=0, column=1, sticky="nsew")
        self.v_help_q, self.v_help_a = tk.StringVar(), tk.StringVar()
        ttk.Label(ans, textvariable=self.v_help_q, style="Big.TLabel", wraplength=500, justify="left").pack(anchor="w")
        ttk.Label(ans, textvariable=self.v_help_a, wraplength=500, justify="left", font=("Helvetica Neue", 13)).pack(anchor="w", pady=(8, 0))
        self._help_answer()
        ttk.Label(t, text=model.about_text().splitlines()[0] if model.about_text() else "", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(10, 0))

    def _help_answer(self) -> None:
        if not hasattr(self, "v_help_q"):
            return
        q, a = HELP_QA[self.v_help_idx.get()]
        self.v_help_q.set(q)
        self.v_help_a.set(a)

    def _reference(self):
        """The long reference text, in its own window."""
        win = tk.Toplevel(self)
        win.title(f"{APP_NAME}: full reference")
        win.geometry("860x640")
        txt = tk.Text(win, font=MONO, wrap="word", padx=14, pady=10)
        ys = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=ys.set)
        try:
            body = (Path(__file__).with_name("HELP.txt")).read_text(encoding="utf-8")
        except OSError:
            body = "See README.md"
        txt.insert("1.0", body + "\n\n" + model.about_text())
        txt.configure(state="disabled")
        theme.style_text(txt)
        ys.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        return win

    # ================================================================== helpers
    def _out(self) -> Path | None:
        s = self.v_output.get().strip()
        return Path(s).expanduser() if s else None

    def _logs(self) -> Path | None:
        o = self._out()
        return o / "_logs" if o else None

    def _open(self, p: Path | None) -> None:
        if p and Path(p).exists():
            open_path(Path(p))
        else:
            messagebox.showinfo(APP_NAME, "Nothing to open yet." if not p else f"Not found:\n{p}")

    def _suggest_confidential(self) -> None:
        """Fill the confidential folder with the suggested sibling whenever it is empty, equal to the output, or still
        the previous suggestion, so the ordinary case needs no thought and the same-folder mistake cannot happen."""
        out = self.v_output.get().strip()
        cur = self.v_confidential.get().strip()
        prev = getattr(self, "_last_suggested", "")
        if out and (not cur or cur == out or cur == prev):
            self._last_suggested = model.suggest_confidential(out)
            self.v_confidential.set(self._last_suggested)

    def _goto_step(self, i: int) -> None:
        self.nb.select(self.tab_run)
        self._show_step(i)

    def _browse(self, var: tk.StringVar, kind: str, filetypes=None) -> None:
        cur = Path(var.get()).expanduser() if var.get().strip() else None
        initial = str(cur if cur and cur.is_dir() else (cur.parent if cur and cur.parent.is_dir() else Path.home()))
        if kind == "dir":
            p = filedialog.askdirectory(initialdir=initial, mustexist=False, title="Choose folder")
        else:
            p = filedialog.askopenfilename(initialdir=initial, filetypes=filetypes or [("All files", "*")], title="Choose file")
        if p:
            var.set(p)

    def _spec(self, dry_run: bool = False) -> JobSpec:
        common = dict(output=self.v_output.get(), resume=self.v_resume.get(), keep_technical=self.v_keep_tech.get(), flat=self.v_flat.get(),
                      dry_run=dry_run, profile=self.v_profile.get(), confidential=self.v_confidential.get())
        if self.v_way.get() == "folder":
            # the opened folder: the engine reads a patient list (and a series selection) this app writes into the
            # confidential folder just before the run. The paths are fixed, so the spec is known before the files exist.
            it, conf = self.intake, self.v_confidential.get().strip()
            if not it or not it.units or not conf:
                return JobSpec(mode="manifest", ctca_only=self.v_choice.get() == "coronary", **common)
            choice = self.v_choice.get()
            sel = str(Path(conf).expanduser() / intake.SELECTION) if self._selection_rows() else ""
            lst = str(Path(conf).expanduser() / ("this_run_" + (intake.PATIENT_ID_MAP if it.mixed else intake.PATIENT_LIST)))
            if it.mixed:
                return JobSpec(mode="mapping", input=str(it.root), mapping=lst, match_on="patientid", series_select=sel, **common)
            return JobSpec(mode="manifest", manifest=lst, ctca_only=choice == "coronary", series_select=sel, **common)
        return JobSpec(mode=self.v_mode.get(), manifest=self.v_manifest.get(), remap=self.v_remap.get(),
                       input=self.v_input.get(), study_id=self.v_study_id.get(), mapping=self.v_mapping.get(),
                       current_col=self.v_current_col.get(), new_col=self.v_new_col.get(), sheet=self.v_sheet.get(),
                       match_on=self.v_match_on.get(), series_pick=self.v_series_pick.get(), ctca_only=self.v_ctca.get(),
                       series_select=self.v_series_select.get(), **common)

    def _spec_key(self) -> tuple:
        s = self._spec()
        sig: tuple = ()
        if self.v_way.get() == "folder" and self.intake:
            sig = (str(self.intake.root), *self._state_sig())
        return (self.v_way.get(), s.mode, s.manifest, s.input, s.study_id, s.mapping, s.output, s.confidential, s.profile, s.ctca_only, s.series_pick,
                s.series_select, s.match_on, s.current_col, s.new_col, sig)

    def _save_settings(self) -> None:
        if self.v_way.get() == "list":      # the list-driven fields are remembered; the folder-first flow has nothing to remember
            model.spec_to_settings(self._spec(), self.settings)
        else:
            self.settings.set("ctca_only", self.v_choice.get() == "coronary")
            self.settings.set("resume", self.v_resume.get())
            self.settings.set("profile", self.v_profile.get())
        self.settings.set("verify_after_run", self.v_verify_after.get())
        self.settings.set("show_all_lines", self.v_show_all.get())
        try:
            self.settings.set("geometry", self.geometry())
        except tk.TclError:
            pass
        self.settings.save()

    def _busy(self) -> bool:
        if self.proc and self.proc.running:
            messagebox.showinfo(APP_NAME, "A job is already running. Stop it first.")
            return True
        return False

    def _set_running(self, running: bool) -> None:
        for b in (self.b_dry, self.b_start, self.b_verify, self.b_share_check, self.b_done_check, self.b_done_hand):
            b.state(["disabled"] if running else ["!disabled"])
        self.b_stop.state(["!disabled"] if running else ["disabled"])
        if not running:
            self._live_validate()
            self._refresh_done()

    # ================================================================== theme, validation, status
    def _apply_theme_colours(self) -> None:
        pal = theme.palette()
        for lbl in getattr(self, "brand_titles", []):
            lbl.configure(foreground=brand.OFF if theme.current() == "dark" else brand.NAVY_DEEP)
        for w in (self.log, self.txt_verify):
            theme.style_text(w)
        ttk.Style(self).configure("Kinds.Treeview", rowheight=THUMB + 6)
        self._card_key = None
        self._refresh_done()
        theme.tag_colours(self.log, ("error", "warn", "ok"))
        for tv in (self.tv_series, self.tv_summary, self.tv_checks, self.tv_logs):
            theme.tag_colours(tv, ("keep", "drop", "check", "block", "error", "ok", "warn"))
        txt = self.lbl_verify.cget("text")
        self.lbl_verify.configure(foreground=pal["ok"] if txt.startswith("PASS") else (pal["error"] if txt.startswith("FAIL") else pal["text"]))
        self._update_strip()

    def _toggle_theme(self) -> None:
        self.theme_mode = theme.toggle(self)
        self.settings.set("theme", self.theme_mode)
        self.b_theme.configure(text="Dark" if self.theme_mode == "light" else "Light")
        self._apply_theme_colours()
        self._update_banner()
        for w in self._viewers:
            try:
                w.apply_theme()
            except (tk.TclError, AttributeError):
                pass

    def _watch_form(self) -> None:
        for v in (self.v_mode, self.v_output, self.v_manifest, self.v_remap, self.v_input, self.v_study_id, self.v_mapping,
                  self.v_current_col, self.v_new_col, self.v_sheet, self.v_match_on, self.v_series_pick, self.v_profile,
                  self.v_ctca, self.v_resume, self.v_keep_tech, self.v_flat, self.v_confidential, self.v_series_select):
            v.trace_add("write", lambda *_: self._schedule_validate())
        self._live_validate()

    def _problems_by_step(self) -> list[list[str]]:
        """What still stops each of the four steps, in order. All empty = ready to run."""
        vp = self._spec().validate()
        # classify by how the message starts, never by a word that might also appear in a folder name
        dest = [p for p in vp if p.startswith(DEST_PROBLEMS)]
        prof = [p for p in vp if p.startswith("Profile file not found")]
        if self.v_way.get() == "list":
            return [[p for p in vp if p not in dest and p not in prof], [], prof, dest]
        it = self.intake
        if not self.v_cases.get():
            s0 = ["Say whether this is one patient or several."]
        elif self._scanning():
            s0 = ["The folder is still being read."]
        elif it is None:
            s0 = ["Choose the folder that holds the scans."]
        elif self._case_mismatch():
            s0 = [self._case_mismatch()]
        elif not it.units:
            s0 = [it.problems[0] if it.problems else "No DICOM scans were found in that folder."]
        else:
            s0 = list(it.problems)
        s1 = ["Nothing would be kept. Tick at least one kind of series."] if it and it.units and not self._plan().n_series else []
        s3 = (intake.id_problems(it) if it and it.units else []) + dest
        out = self._out()
        if it and it.units and out:
            try:
                o, r = out.resolve(), it.root.resolve()
                if o == r or r in o.parents:
                    s3.append("The output folder must not be inside the folder of scans.")
            except OSError:
                pass
        return [s0, s1, prof, s3]

    def _schedule_validate(self) -> None:
        if self._validate_id:
            self.after_cancel(self._validate_id)
        self._validate_id = self.after(200, self._live_validate)

    def _live_validate(self) -> None:
        self._validate_id = None
        if not hasattr(self, "lbl_ready"):
            return
        steps = self._problems_by_step()
        problems = [p for s in steps for p in s]
        so_far = [p for s in steps[:self.step + 1] for p in s] if self.step <= SAVE_STEP else []
        running = bool(self.proc and self.proc.running)
        folder = self.v_way.get() == "folder"
        top = getattr(self, "_top_next", {}).get(self.step)
        if top is not None:
            if self.step == SAVE_STEP:      # "Results": there is something to look at as soon as an output folder exists
                top.state(["!disabled"] if self._out() and self._out().is_dir() else ["disabled"])
            else:
                top.state(["!disabled"] if not so_far else ["disabled"])
        self.v_nav_reason.set(("Next needs: " + so_far[0]) if so_far and self.step < SAVE_STEP else "")
        # in the folder-first flow the scan and the series list are the preview; a list-driven run still needs a dry run first
        previewed = folder or self.previewed_key == self._spec_key()
        if problems:
            self.v_ready.set("Before you can start: " + "  \u00b7  ".join(problems[:3]))
            self.lbl_ready.configure(style="Warn.TLabel")
        elif not previewed:
            self.v_ready.set("Run Preview first. It writes nothing and shows what would happen.")
            self.lbl_ready.configure(style="Muted.TLabel")
        else:
            self.v_ready.set("Ready. Press Anonymise." if folder else "Previewed. Anonymise when you are happy with what you saw.")
            self.lbl_ready.configure(style="Ok.TLabel")
        if not running:
            self.b_dry.state(["!disabled"] if not problems else ["disabled"])
            self.b_start.state(["!disabled"] if not problems and previewed else ["disabled"])
        self.v_summary.set(self._summary_text(problems))
        self.v_summary_short.set("" if problems else "  ".join(self.v_summary.get().splitlines()[:2]))
        self.v_drive_note.set(self._drive_note())
        if self.step == 2:
            self._refresh_change_cards()
        self._update_banner()

    def _summary_text(self, problems: list[str]) -> str:
        s = self._spec()
        if problems:
            return "Complete the steps above to see what will happen."
        prof = model.profile_for_path(s.profile)
        kept = [KEPT_WORDS.get(k, k) for k in prof.kept_summary()]
        removing = "everything identifying" if not kept else "everything identifying except " + ", ".join(kept)
        if self.v_way.get() == "folder" and self.intake:
            it, pl = self.intake, self._plan()
            n = len(it.units)
            what = {"coronary": "coronary CT only", "all": "every series", "choose": "the series you ticked"}[self.v_choice.get()]
            return "\n".join([f"Anonymise {n} {it.noun(n)} from {it.root.name}: {what} ({pl.n_series} series, {pl.n_images:,} images).",
                              f"Removing {removing}.",
                              f"Copies go to: {s.output}",
                              f"The confidential key goes to: {s.confidential}",
                              "The original scans are not changed."])
        if s.mode == "manifest":
            n = model.manifest_count(s.manifest)
            who = f"{n} patients from {Path(s.manifest).name}" if n is not None else f"the patients in {Path(s.manifest).name}"
        elif s.mode == "single":
            who = f"one patient from {Path(s.input).name}, to be called {s.study_id.strip()}"
        else:
            who = f"the patients in {Path(s.input).name}, renamed by {Path(s.mapping).name}"
        opts = []
        if s.mode == "manifest" and s.ctca_only:
            opts.append("coronary series only")
        if s.mode == "manifest" and s.resume:
            opts.append("skip patients already done")
        if s.series_select.strip():
            from .preview import read_selection
            sel = read_selection(Path(s.series_select))
            opts.append(f"only the ticked series for {len(sel)} patient(s)")
        lines = [f"Anonymise {who}" + (f" ({', '.join(opts)})" if opts else "") + ".",
                 f"Removing {removing}.",
                 f"Copies go to: {s.output}",
                 f"The confidential key goes to: {s.confidential}",
                 "The original scans are not changed."]
        return "\n".join(lines)

    def _drive_note(self) -> str:
        s = self._spec()
        src = s.manifest if s.mode == "manifest" else s.input
        if self.v_way.get() == "folder":
            src = str(self.intake.root) if self.intake else ""
        if not s.output.strip() or not src.strip():
            return ""
        try:
            a, b = Path(s.output).expanduser().resolve(), Path(src).expanduser().resolve()
        except OSError:
            return ""
        key = lambda p: p.parts[:3] if len(p.parts) >= 3 and p.parts[1] == "Volumes" else (p.anchor,)
        external = len(key(b)) == 3 or (os.name == "nt" and b.anchor.upper() != os.environ.get("SystemDrive", "C:").upper() + "\\")
        if key(a) == key(b) and external:
            return "The output is on the same drive as the scans. That works, but a separate drive keeps the anonymised copies apart from the originals."
        return ""

    def _update_banner(self) -> None:
        if not hasattr(self, "banner"):
            return
        prof = model.profile_for_path(self.v_profile.get())
        kept = [KEPT_WORDS.get(k, k) for k in prof.kept_summary()]
        if kept:
            self.v_banner.set(f"Kept in the output: {', '.join(kept)}. It is not fully blinded.")
            self.banner.configure(bg=brand.AMBER, fg="#1f2328")
            if not self.banner.winfo_ismapped():
                self.banner.pack(fill="x", padx=16, pady=(4, 0), after=self.strip)
        else:
            self.banner.pack_forget()

    def _update_strip(self, text: str | None = None, level: str | None = None) -> None:
        if not hasattr(self, "strip"):
            return
        pal = theme.palette()
        if text is None:
            out = self._out()
            if self.proc and self.proc.running:
                text, level = self.v_progress_text.get() or f"{self.job or 'Job'} running...", "info"
            elif not out or not out.is_dir():
                text, level = "Not started", "muted"
            else:
                checks = model.share_readiness(out)
                levels = {c.level for c in checks}
                done = any(c.title[:1].isdigit() and c.title.endswith("completed studies") for c in checks)
                if "block" in levels:
                    if any(c.title.startswith("Verify FAILED") for c in checks):
                        text, level = "Check failed: identifiers found. Do not share.", "error"
                    elif not done:
                        text, level = "Not started", "muted"
                    else:
                        text, level = "Anonymised, not yet safe to hand over: " + next(c.title for c in checks if c.level == "block"), "warn"
                elif "warn" in levels:
                    text, level = "Verified. The results screen lists what still needs a look before handing over.", "warn"
                else:
                    text, level = "Verified. Safe to hand over.", "ok"
        colours = {"muted": (pal["border"], pal["text"]), "info": (brand.NAVY, "#ffffff"), "ok": (brand.TEAL_DARK, "#ffffff"),
                   "warn": (brand.AMBER, "#1f2328"), "error": (brand.RED, "#ffffff")}
        bg, fg = colours.get(level or "muted", colours["muted"])
        self.v_strip.set(text)
        self.strip.configure(bg=bg, fg=fg)

    # ================================================================== jobs
    def _start_job(self, job: str, args: list[str], log_name: str | None, title: str) -> None:
        if self._busy():
            return
        cmd = model.engine_command(args)
        self.log_path = None
        if log_name:
            # the window's own log repeats the engine's output, which can name original folders: it belongs with the
            # confidential material, and only falls back to the output's _logs when no confidential folder is set
            logs = self._conf() or self._logs()
            if logs:
                self.log_path = logs / f"app_{log_name}_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        self._clear_log()
        self.f_prog.grid()
        if job != "verify":
            self._hide_card()
        self.skipped_hidden = 0
        self.prog_state = {"n": 0, "total": 0, "study": "", "files": 0, "expected": None, "known": False}
        self._bar_mode(False)
        self.v_progress.set(0)
        self.v_progress_text.set(f"{title} started")
        self.v_status.set(f"{title} running...")
        self._append_log("$ " + shown_command(cmd), "plain")
        self.proc = EngineProcess(cmd, self.log_path)
        try:
            self.proc.start()
        except OSError as e:
            self.proc = None
            messagebox.showerror(APP_NAME, f"Could not start the engine:\n{e}")
            return
        self.job = job
        self._set_running(True)
        self._update_strip(f"{title} running...", "info")
        self._save_settings()
        self.nb.select(self.tab_run)
        if job in ("verify", "recheck", "thick") and self.step != DONE_STEP:
            self._show_step(DONE_STEP)
        if job == "thick" and not self.v_show_log.get():       # this job's answer is its output: show it
            self.v_show_log.set(True)
            self._toggle_log()

    def _earlier_copies(self) -> list[tuple[intake.Unit, str]]:
        """Studies of this run that are already in the output under a different ID: the same scan anonymised before,
        then renamed. Read before the lists are rewritten, which would forget the earlier ID."""
        it, conf, out = self.intake, self.v_confidential.get().strip(), self._out()
        if self.v_way.get() != "folder" or not it or not conf or not out or not out.is_dir():
            return []
        existing = intake.read_patient_list(conf, it.mixed)
        found = []
        for u in it.units:
            old = existing.get(intake.match_key(it, u), "")
            if old and intake.safe_name(old) != intake.safe_name(u.new_id) and (out / intake.safe_name(old)).is_dir():
                found.append((u, old))
        return found

    def _remove_earlier(self, earlier: list[tuple[intake.Unit, str]]) -> None:
        """Delete the earlier copies from the OUTPUT folder (never the originals), so the renamed run replaces them."""
        out = self._out()
        for _u, old in earlier:
            for d in (out / intake.safe_name(old), out / "_review" / intake.safe_name(old)):
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
            self._append_log(f"removed the earlier copy {old} from the output; it is replaced by this run", "warn")

    def _start_run(self, dry_run: bool = False) -> None:
        folder = self.v_way.get() == "folder"
        earlier: list = []
        if folder:
            problems = [p for s in self._problems_by_step() for p in s]
            if problems:
                messagebox.showerror("Cannot start", "\n".join(problems))
                return
            earlier = self._earlier_copies() if not dry_run else []
        replace = False
        if not dry_run and self.demo_stage is None:
            if not folder and self.previewed_key != self._spec_key():
                messagebox.showinfo("Preview first", "Run Preview first. It writes nothing and shows what would happen, so a wrong folder is caught before anything is written.")
                return
            ok, replace = self._confirm_run(earlier)
            if not ok:
                return
        if folder and not self._materialise():
            return
        spec = self._spec(dry_run)
        problems = spec.validate()
        if problems:
            messagebox.showerror("Cannot start", "\n".join(problems))
            return
        if earlier and replace:
            self._remove_earlier(earlier)
        self._start_job("dry" if dry_run else "run", ["run", *spec.run_args()], None if dry_run else "run", "Preview" if dry_run else "Anonymisation")

    def _confirm_rows(self) -> tuple[str, list[tuple[str, str, str]]]:
        """(heading, [(label, value, full text for a tooltip)]) for the 'ready to anonymise?' window: what, how much,
        what goes, and the two destinations with their paths shortened."""
        s = self._spec()
        prof = model.profile_for_path(s.profile)
        kept = [KEPT_WORDS.get(k, k) for k in prof.kept_summary()]
        removing = "Everything that identifies" if not kept else "Everything that identifies, except " + ", ".join(kept)
        if self.v_way.get() == "folder" and self.intake:
            it, pl = self.intake, self._plan()
            n = len(it.units)
            what = {"coronary": "Coronary CT only", "all": "Every series", "choose": "The series you ticked"}[self.v_choice.get()]
            heading = f"Anonymise {n} {it.noun(n)}?"
            rows = [("From", short_path(it.root), str(it.root)),
                    ("Keeping", f"{what}: {pl.n_series} series, {pl.n_images:,} images", "")]
            if pl.empty:
                rows.append(("Skipping", f"{len(pl.empty)} with nothing to keep", ", ".join(pl.empty[:40])))
        else:
            if s.mode == "manifest":
                n = model.manifest_count(s.manifest)
                heading = f"Anonymise {n} patients?" if n is not None else "Anonymise the patients in the list?"
                rows = [("From the list", short_path(s.manifest), s.manifest),
                        ("Keeping", "Coronary CT only" if s.ctca_only else "Every series", "")]
            else:
                heading = "Anonymise the patients in this folder?"
                rows = [("From", short_path(s.input), s.input), ("New IDs from", short_path(s.mapping), s.mapping)]
        rows += [("Removing", removing, "")]
        if self.v_way.get() == "folder" and self.intake and len(self.intake.units) == 1:
            rows.append(("New ID", self.intake.units[0].new_id.strip(), ""))
        rows += [("Copies go to", short_path(s.output), s.output),
                 ("The key goes to", short_path(s.confidential), s.confidential)]
        return heading, rows

    def _confirm_run(self, earlier: list | None = None) -> tuple[bool, bool]:
        """(go ahead, replace the earlier copies). Earlier copies are the same scans already in the output under
        another ID: the window says so, and offers to remove them first so the output does not hold both."""
        heading, rows = self._confirm_rows()
        option = ""
        earlier = earlier or []
        if earlier:
            olds = ", ".join(old for _u, old in earlier[:5]) + (" ..." if len(earlier) > 5 else "")
            rows.append(("Already there", (f"{olds} is an earlier copy of this scan, under its old ID" if len(earlier) == 1
                                           else f"{len(earlier)} of these scans are already in the output under old IDs: {olds}"), ""))
            option = ("Remove the earlier copy from the output first, so only the new ID remains" if len(earlier) == 1
                      else f"Remove the {len(earlier)} earlier copies from the output first, so only the new IDs remain")
        win = ConfirmRun(self, heading, rows, "Your original scans are not changed.", option=option)
        ok = win.wait()
        return ok, bool(option) and win.v_option.get()

    def _start_verify(self) -> None:
        out = self._out()
        if not out or not out.is_dir():
            messagebox.showerror(APP_NAME, "Choose an existing output folder first.")
            return
        args = ["verify", *model.verify_args(str(out), model.split_needles(self.v_needles.get()), self.v_keep_tech.get(), self.v_profile.get(), self.v_confidential.get())]
        self._start_job("verify", args, "verify", "Output check")

    def _handover(self) -> None:
        """Re-hash every output file against the manifest written at verification; only an unchanged tree is opened."""
        out = self._out()
        if not out or not out.is_dir():
            return
        self._start_job("recheck", ["verify", *model.recheck_args(str(out))], "recheck", "Hand-over re-check")

    def _start_thick(self, fix: bool) -> None:
        out = self._out()
        if not out or not out.is_dir():
            messagebox.showerror(APP_NAME, "Choose an existing output folder first.")
            return
        if fix and not confirm_typed(self, "Remove thick studies from the output",
                                     f"This deletes, from the OUTPUT folder only, every finished patient whose kept series are thicker than "
                                     f"{model.MAX_SLICE_MM:g} mm, so the next run with 'Skip patients already done' redoes them.\n\nOriginal scans are not touched.\n\nOutput: {out}"):
            return
        self._start_job("thick", ["thick", *model.thick_args(str(out), fix)], "thick", "Thickness audit")

    def _stop(self) -> None:
        if not (self.proc and self.proc.running):
            return
        if self.job == "run" and not messagebox.askyesno("Stop?", "The patient in progress is left unfinished and will be redone next time with 'Skip patients already done' ticked.\n\nStop now?"):
            return
        self.demo_stage = None
        self.proc.stop()
        self._append_log("** stopped by user **", "warn")

    def _finish(self) -> None:
        assert self.proc is not None
        rc = self.proc.returncode
        secs = self.proc.elapsed()
        took = f"{secs / 60:.1f} min" if secs >= 90 else f"{secs:.0f} s"
        job, self.job = self.job, None
        titles = {"run": "Anonymisation", "dry": "Preview", "verify": "Output check", "thick": "Thickness audit", "recheck": "Hand-over re-check"}
        title = titles.get(job or "", "Job")
        log_text = self.log.get("1.0", "end")
        stopped = log_text.rstrip().endswith("stopped by user **")
        self._bar_mode(True)
        if rc == 0:
            self.v_progress_text.set(f"{title} finished in {took}")
            self.v_status.set(f"{title} finished")
            if job == "run":
                self.v_progress.set(100)
            if job == "dry":
                self.previewed_key = self._spec_key()
                summary = next((l.strip() for l in reversed(log_text.splitlines()) if l.strip().startswith("DRY RUN")), "Preview finished.")
                self._show_card("Preview done: nothing was written", summary + "  Look at the images and the header before/after in the viewer, then press Anonymise.",
                                "Open viewer", lambda: self._viewer("source"))
            if job == "verify":
                try:        # the certificate travels with the output from the moment it is verified
                    certificate.write_certificate(self._out())
                except OSError:
                    pass
        elif stopped:
            self.v_progress_text.set(f"{title} stopped after {took}")
            self.v_status.set(f"{title} stopped")
        else:
            self.v_progress_text.set(f"{title} finished with problems after {took}")
            self.v_status.set(f"{title}: problems")
        self._set_running(False)
        self._refresh_all()
        self._recovery(job, rc, stopped, log_text)
        if self.skipped_hidden and not self.v_show_all.get():
            self._append_log(f"({self.skipped_hidden} lines for patients skipped as already done are hidden; More > Show to see them)", "plain")
        # chaining: automatic check after a run, and the demo's three stages
        if job == "recheck":
            self._goto_step(DONE_STEP)
            if rc == 0:
                self._show_card("Ready to hand over", "Every output file is byte-for-byte as it was when verified. The attestation and checksum manifest in _logs travel with it.",
                                "Open output folder", lambda: self._open(self._out()))
            else:
                self._show_card("Do not hand over", "Files changed, went missing or were added since verification. Run the check again, then hand over.",
                                "Check now", self._start_verify)
        chained = job == "run" and rc == 0 and self.v_verify_after.get()
        if chained:
            self.after(400, self._start_verify)
        elif job in ("run", "verify") and not stopped:
            # the journey ends on the results screen, whatever the result; someone who walked away is told
            self._goto_step(DONE_STEP)
            try:
                away = self.focus_displayof() is None
            except (KeyError, tk.TclError):
                away = False
            if secs >= 20 or away:
                self._notify(self.v_done_head.get() or f"{title} finished")
        if self.demo_stage == "run" and job == "run" and rc == 0:
            self.demo_stage = "verify"
        elif self.demo_stage == "verify" and job == "verify":
            self.demo_stage = None
        elif self.demo_stage and (rc != 0 or stopped):
            self.demo_stage = None

    def _recovery(self, job: str | None, rc: int | None, stopped: bool, log_text: str) -> None:
        if rc == 0 or stopped:
            return
        if "no longer reachable" in log_text or "drive disappeared" in log_text or "Could not write the completion marker" in log_text:
            self._show_card("The output drive disconnected", "Completed patients are safe. Reconnect the drive, then press Resume: the run continues where it stopped.",
                            "Resume", lambda: (self.v_resume.set(True), self._start_run()))
        elif job == "verify":
            self._show_card("Identifiers were found in the output", "Do not share it. The report names each file and tag. If it is a word you typed that is also a common word, refine it; if it is a real leak, report it.",
                            "Open report", lambda: self._show_details("verify"))
        elif "folder not found" in log_text and "Done in" in log_text:
            self._show_card("Some folders in the list were not found", "The run finished, but the patients whose folders are missing were skipped. Check the paths in the list (or the drive path fix) and run again with 'Skip patients already done'.",
                            "Go to step 1", lambda: self._goto_step(0))
        else:
            self._show_card(f"{'Preview' if job == 'dry' else 'The run'} did not finish", "The last lines of the activity log say why. Copy them if you need to ask for help.",
                            "Copy log", self._copy_log)

    def _show_card(self, title: str, text: str, button: str, command, button2: str | None = None, command2=None) -> None:
        self.v_card_title.set(title)
        self.v_card_text.set(text)
        self.b_card.configure(text=button, command=command)
        if button2:
            self.b_card2.configure(text=button2, command=command2)
            self.b_card2.grid()
        else:
            self.b_card2.grid_remove()
        self.card.grid()

    def _hide_card(self) -> None:
        self.card.grid_remove()

    def _poll(self) -> None:
        self._poll_scan()
        if self.proc is not None and self.job is not None:
            for line in self.proc.poll_lines():
                self._handle_line(line)
            if self.proc.done:
                for line in self.proc.poll_lines():
                    self._handle_line(line)
                self._finish()
        self.after(POLL_MS, self._poll)

    def _bar_mode(self, known: bool) -> None:
        """Determinate when a percentage is known, pulsing otherwise."""
        try:
            if known:
                self.bar.stop()
                self.bar.configure(mode="determinate")
            else:
                self.bar.configure(mode="indeterminate")
                self.bar.start(12)
        except tk.TclError:
            pass

    def _handle_line(self, line: str) -> None:
        if model.is_skipped_line(line) and not self.v_show_all.get():
            self.skipped_hidden += 1
            return
        verb = "Anonymising" if self.job == "run" else "Previewing"
        st = self.prog_state
        start = model.parse_start(line)
        beat = model.parse_heartbeat(line)
        prog = model.parse_progress(line)
        if start:
            st.update(n=start[0], total=start[1], study=start[2], files=0, expected=start[3])
            self._set_progress(verb)
        elif beat:
            st.update(study=beat[0], files=beat[1])
            self._set_progress(verb)
        elif prog:
            st.update(n=prog.done, total=prog.total, study=prog.study, files=0, expected=None, known=True)
            self._bar_mode(True)
            self.v_progress.set(prog.percent)
            self.v_progress_text.set(prog.text)
            self._update_strip(f"{verb} patient {prog.done} of {prog.total} done" + (f", about {prog.eta_minutes} min left" if prog.eta_minutes else ""), "info")
        self._append_log(line, model.classify_line(line))

    def _set_progress(self, verb: str) -> None:
        st = self.prog_state
        files = st.get("files", 0)
        exp = st.get("expected")
        n, total = st.get("n", 0), st.get("total", 0)
        where = f"patient {n} of {total}: {st.get('study', '')}" if total else st.get("study", "")
        left = lambda pct: (lambda t: f"  \u00b7  {t}" if t else "")(model.eta_text(self.proc.elapsed() if self.proc else 0.0, pct / 100))
        if total and exp:
            pct = ((n - 1) + min(files, exp) / exp) / total * 100
            self._bar_mode(True)
            self.v_progress.set(pct)
            text = f"{verb} {where}, {files:,} of {exp:,} files" + left(pct)
        elif exp:
            self._bar_mode(True)
            self.v_progress.set(min(files, exp) / exp * 100)
            text = f"{verb} {where}, {files:,} of {exp:,} files" + left(min(files, exp) / exp * 100)
        else:
            self._bar_mode(False)
            text = f"{verb} {where}, {files:,} files so far"
        self.v_progress_text.set(text)
        self._update_strip(text, "info")

    # ------------------------------------------------------------------ log widget
    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _append_log(self, line: str, tag: str = "plain") -> None:
        at_end = self.log.yview()[1] >= 0.999
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n", tag if tag != "plain" else ())
        self.log.configure(state="disabled")
        if at_end:
            self.log.see("end")

    def _copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.log.get("1.0", "end"))
        self.v_status.set("Log copied to clipboard")

    def _show_command(self) -> None:
        spec = self._spec()
        problems = [p for s in self._problems_by_step() for p in s]
        text = shown_command(spec.command())
        if problems:
            text += "\n\nNot runnable yet:\n- " + "\n- ".join(problems)
        elif self.v_way.get() == "folder":
            text += "\n\nThe patient list and the series selection named here are written into the confidential folder when the run starts."
        messagebox.showinfo("Command", text)

    # ------------------------------------------------------------------ profiles, viewer
    def _refresh_profile_label(self) -> None:
        self._profile_entries = model.list_profiles()
        self.cb_profile["values"] = [e.label for e in self._profile_entries]
        cur = self.v_profile.get().strip()
        match = next((e for e in self._profile_entries if (str(e.path) if e.path else "") == cur), None)
        if match is None:
            match = self._profile_entries[0] if self._profile_entries else None
            self.v_profile.set("")
        if match:
            self.v_profile_label.set(match.label)
            self.v_profile_desc.set(match.profile.describe() + "  Written into every file as: " + match.profile.method_string(model.ENGINE_VERSION))
            kept = [KEPT_WORDS.get(k, k) for k in match.profile.kept_summary()]
            self.v_status.set("Removes everything identifying." if not kept else "Removes everything identifying except " + ", ".join(kept) + ".")
        self._update_banner()

    def _profile_chosen(self) -> None:
        label = self.v_profile_label.get()
        e = next((e for e in self._profile_entries if e.label == label), None)
        if e:
            self.v_profile.set(str(e.path) if e.path else "")
            self._refresh_profile_label()

    def _edit_profiles(self) -> None:
        ProfileEditor(self, self.v_profile.get())

    def ticks_saved(self, n: int, study_id: str) -> None:
        """Called by the viewer after 'Anonymise only the ticked series': looking at the series in the viewer is the
        preview, so land on the last step with the button enabled."""
        if self.v_way.get() == "folder" and self.intake:
            from .preview import read_selection, selection_path
            sel = read_selection(selection_path(self.v_confidential.get(), ""))
            if study_id in sel:
                self.overrides[study_id] = set(sel[study_id])
            self._fill_kinds()
        self.previewed_key = self._spec_key()
        self._goto_step(SAVE_STEP)
        self._live_validate()
        self.v_ready.set(f"{n} ticked series saved for {study_id}. Press Anonymise.")
        self.lbl_ready.configure(style="Ok.TLabel")
        self.lift()
        self.b_start.focus_set()

    def _about(self) -> None:
        win = tk.Toplevel(self)
        win.title(f"About {APP_NAME}")
        win.transient(self)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=24)
        f.pack(fill="both", expand=True)
        brand.Wordmark(f, size=26, with_mark=96).pack(anchor="w")
        ttk.Label(f, text=model.about_text(), justify="left", wraplength=520).pack(anchor="w", pady=(14, 16))
        ttk.Button(f, text="Close", command=win.destroy).pack(anchor="e")

    def _viewer(self, mode: str, study_id: str | None = None) -> None:
        if mode == "source" and self.v_way.get() == "folder":
            if self._scanning():
                return
            if not self.intake or not self.intake.units:
                messagebox.showinfo(APP_NAME, "Open a folder of scans first.")
                self._goto_step(0)
                return
            if not self._materialise():      # the viewer lists the patients from the same list the run will use
                return
        w = open_viewer(self, mode, study_id)
        if w is not None:
            self._viewers.append(w)

    def _viewer_selected_series(self) -> None:
        sel = self.tv_series.selection()
        sid = self.tv_series.set(sel[0], "study_id") if sel else None
        self._viewer("source", sid)

    # ================================================================== refresh
    def _refresh_all(self) -> None:
        self._refresh_home()
        self._refresh_output_status()
        self._refresh_series()
        self._refresh_verify()
        self._refresh_share()
        self._refresh_done()
        self._update_strip()

    def _schedule_output_refresh(self) -> None:
        if self._out_refresh_id:
            self.after_cancel(self._out_refresh_id)
        self._out_refresh_id = self.after(500, self._refresh_all)

    def _refresh_output_status(self) -> None:
        out = self._out()
        if not out:
            self.v_out_status.set("")
            return
        if not out.is_dir():
            self.v_out_status.set("This folder does not exist yet; it will be created on the first run.")
            return
        done, partial = model.study_state(out)
        n = model.manifest_count(self.v_manifest.get()) if self.v_mode.get() == "manifest" else None
        if self.v_way.get() == "folder":
            n = len(self.intake.units) if self.intake else None
        s = f"Patients done: {len(done)}   ·   half-finished (will be redone): {len(partial)}"
        if n is not None:
            s += f"   ·   in this run: {n}"
        self.v_out_status.set(s)

    def _conf(self) -> Path | None:
        s = self.v_confidential.get().strip()
        return Path(s).expanduser() if s else None

    def _refresh_series(self) -> None:
        rows: list[dict] = []
        for d in (self._conf(), self._logs()):
            if d and d.is_dir():
                rows += model.load_series_rows(d)
        self.series_rows = rows
        rows = model.filter_series(self.series_rows, self.v_series_filter.get(), self.v_series_query.get())
        tv = self.tv_series
        tv.delete(*tv.get_children(""))
        for r in rows:
            tag = "check" if model.needs_check(r) else ("keep" if r.get("decision") == "keep" else "drop")
            tv.insert("", "end", values=[r.get(c, "") for c in tv["columns"]], tags=(tag,))
        c = model.series_counts(self.series_rows)
        self._toggle_empty(tv, self.empty_series, not self.series_rows)
        self.lbl_series.configure(text=(f"{len(rows)} shown of {len(self.series_rows)} series across {c['studies']} patients: "
                                        f"{c['kept']} kept, {c['dropped']} dropped, {c['check']} need a look") if self.series_rows else "")

    def _refresh_verify(self) -> None:
        logs = self._logs()
        status, rep, txt = model.verify_status(logs) if logs and logs.is_dir() else (None, None, "")
        pal = theme.palette()
        if status == "PASS":
            self.lbl_verify.configure(text=f"PASS  ({rep.name})", foreground=pal["ok"])
        elif status == "FAIL":
            self.lbl_verify.configure(text=f"FAIL: residual identifiers found  ({rep.name})", foreground=pal["error"])
        else:
            self.lbl_verify.configure(text="Not checked yet.", foreground=pal["text"])
        self.txt_verify.configure(state="normal")
        self.txt_verify.delete("1.0", "end")
        self.txt_verify.insert("1.0", txt[-20000:])
        self.txt_verify.configure(state="disabled")
        tv = self.tv_summary
        tv.delete(*tv.get_children(""))
        summ = model.latest(logs, "summary", (".csv",)) if logs and logs.is_dir() else None
        for r in (model.read_csv(summ) if summ else []):
            st = r.get("status", "")
            tag = "keep" if st == "OK" else ("check" if st in ("EMPTY", "NO CTCA SERIES") else "drop" if not st else "error")
            tv.insert("", "end", values=[r.get(c, "") for c in tv["columns"]], tags=(tag,))
        has_output = bool(logs and logs.is_dir() and (status or summ))
        if has_output:
            self.empty_verify.grid_remove()
            self.verify_frame.grid()
        else:
            self.verify_frame.grid_remove()
            self.empty_verify.grid()

    def _refresh_share(self) -> None:
        out, logs = self._out(), self._logs()
        tv = self.tv_checks
        tv.delete(*tv.get_children(""))
        checks = model.share_readiness(out, self.v_confidential.get()) if out else []
        has_output = bool(out and out.is_dir())
        for c in checks:
            tv.insert("", "end", values=[LEVEL_MARK.get(c.level, ""), c.title, c.detail], tags=(c.level,))
        self._toggle_empty(tv, self.empty_share, not has_output)
        blocks = [c for c in checks if c.level == "block"]
        self.b_handover.state(["!disabled"] if has_output and not blocks else ["disabled"])
        self.lbl_handover.configure(text=("Hand-over is blocked until: " + "; ".join(c.title for c in blocks)) if blocks else "")
        tl = self.tv_logs
        tl.delete(*tl.get_children(""))
        if logs and logs.is_dir():
            for p in sorted(logs.iterdir()):
                if not p.is_file() or p.name.startswith("."):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                size = f"{st.st_size / 1024:.0f} KB" if st.st_size >= 1024 else f"{st.st_size} B"
                mod = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                if p.name in model.LOG_KEEP or model.is_pass_report(p):
                    note, tag = "stays here: re-runs need it", "keep"
                elif p.name.upper().startswith("LINKAGE_"):
                    note, tag = "CONFIDENTIAL: study ID -> patient", "block"
                else:
                    note, tag = "confidential: original paths / descriptions", "check"
                tl.insert("", "end", values=[p.name, size, mod, note], tags=(tag,))

    def _export_series(self) -> None:
        rows = model.filter_series(self.series_rows, self.v_series_filter.get(), self.v_series_query.get())
        if not rows:
            messagebox.showinfo(APP_NAME, "No rows to export.")
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="series_decisions.csv", filetypes=[("CSV", "*.csv")])
        if p:
            model.write_csv(Path(p), rows, list(self.tv_series["columns"]))
            self.v_status.set(f"Exported {len(rows)} rows")

    def _move_logs(self) -> None:
        out = self._out()
        if not out or not out.is_dir():
            messagebox.showerror(APP_NAME, "Choose an existing output folder first.")
            return
        if self._busy():
            return
        dest = filedialog.askdirectory(title="Choose a folder OUTSIDE the output tree for the confidential logs", mustexist=True,
                                       initialdir=self.v_confidential.get().strip() or str(Path.home()))
        if not dest:
            return
        try:
            moved, folder = model.move_logs_out(out, Path(dest))
        except (ValueError, OSError) as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        self._refresh_all()
        messagebox.showinfo("Logs moved", f"{len(moved)} file(s) moved to\n{folder}\n\nuid_salt.txt stayed in _logs so re-runs keep the same UIDs. "
                                          "Keep the moved folder with the linkage information, away from the scans.")

    # ================================================================== demo and first run
    def _run_demo(self, auto: bool = False) -> None:
        """Load two made-up patients into the ordinary flow. The user walks the four steps, or presses 'Play it for me'
        (auto=True does that straight away, for the self-test)."""
        if self._busy() or self._scanning():
            return
        try:
            from scrubdicom import demo_data
        except ImportError as e:
            messagebox.showerror(APP_NAME, f"The demo needs numpy, which is not installed: {e}")
            return
        base = model.settings_path().parent / "sample"
        try:
            shutil.rmtree(base, ignore_errors=True)      # a fresh demo every time: synthetic data only, nothing to keep
            demo_data.main(base / "scans")
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Could not create the demo patients: {e}")
            return
        self.demo_stage = None
        self._hide_card()
        self.v_listway.set(False)
        self.v_cases.set("many")
        self._apply_way()
        self._auto_dirs = ("", "")
        self.v_output.set(str(base / "anonymised"))
        self.v_confidential.set(str(base / "confidential"))
        self.v_prefix.set("DEMO")
        self.v_strip_choice.set("default")
        self._strip_changed()
        self.v_resume.set(True)
        self.v_verify_after.set(True)
        self._goto_step(0)
        self._open_folder(str(base / "scans"), wait=True)
        self._auto_dirs = (self.v_output.get(), self.v_confidential.get())    # the next folder opened gets its own suggestion
        self.v_choice.set("coronary")
        self._choice_changed()
        if auto:
            self._demo_play()
        else:
            self._show_card("Demo: two made-up patients are loaded",
                            "Their folders are named after them, and every scan carries a name, a date of birth and a hospital number. "
                            "Press Next to see what you can keep and remove, or let it play.",
                            "Play it for me", self._demo_play)

    def _demo_play(self) -> None:
        self.demo_stage = "run"
        self._goto_step(SAVE_STEP)
        self.after(300, self._start_run)

    # ================================================================== lifecycle
    def _on_close(self) -> None:
        if self.proc and self.proc.running:
            if not messagebox.askyesno("Quit?", "A job is running. Stop it and quit?"):
                return
            self.proc.stop()
        self._scan_cancel = True
        self._save_settings()
        self.destroy()

    def _on_exception(self, exc_type, exc, tb) -> None:
        path = model.record_error(exc_type, exc, tb)
        try:
            self._show_card("Something went wrong", f"{exc_type.__name__}: {model.redact_paths(str(exc))[:200]}\nDetails (with folder names removed) were saved to {path.name if path else 'the error log'}.",
                            "Open error log", lambda: self._open(path))
            self.nb.select(self.tab_run)
        except Exception:
            pass

    def _selftest_walk(self) -> None:
        # a dialog would block an unattended self-test for ever: record it and fail instead
        self._selftest_dialogs: list[str] = []

        def recorded(title="", message="", **_k):
            self._selftest_dialogs.append(f"{title}: {message}")
            return False

        messagebox.showerror = messagebox.showinfo = messagebox.askokcancel = messagebox.askyesno = recorded
        for tab in (self.tab_home, self.tab_run, self.tab_help):
            self.nb.select(tab)
            self.update()
        for which in ("series", "verify", "share"):
            self._show_details(which)
            self.update()
        self.nb.select(self.tab_run)
        self.v_show_log.set(True)
        self._toggle_log()
        self.update()
        self.v_show_log.set(False)
        self._toggle_log()
        for i in range(len(STEP_TITLES)):
            self._show_step(i)
            self.update()
        self.v_listway.set(True)
        self._apply_way()
        for mode in model.MODES:
            self.v_mode.set(mode)
            self._apply_mode()
            for i in range(len(STEP_TITLES)):
                self._show_step(i)
                self.update()
        self.v_mode.set("manifest")
        self.v_listway.set(False)
        self._apply_way()
        self.nb.select(self.tab_run)
        self._show_step(0)
        ref = self._reference()
        self.update()
        ref.destroy()
        self.update()
        for mode in ("source", "output"):
            w = open_viewer(self, mode)
            if w is not None:
                self.update()
                w.destroy()
        ed = ProfileEditor(self, "")
        self.update()
        ed.lb.selection_clear(0, "end")
        ed.lb.selection_set(1)
        ed._pick()
        self.update()
        ed.destroy()
        print("selftest: window built, all tabs and steps drawn, viewer opened, profile editor opened", flush=True)
        print(f"selftest: drag-and-drop {'available' if self.dnd_ok else 'not available on this build (the Choose button still works)'}", flush=True)
        self._selftest_viewer_on_synthetic_patients()
        self._selftest_demo()
        self.after(200, self.destroy)

    def _selftest_demo(self) -> None:
        """Run the built-in demo end to end (open the folder, anonymise, check) inside the self-test, so a frozen build
        proves the whole flow. Sample data goes to a temporary folder, not the user's settings folder."""
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="scrubdicom_demo_"))
        real = model.settings_path
        model.settings_path = lambda: tmp / "settings.json"   # redirect the demo's sample folder
        try:
            self._run_demo(auto=True)
            if not self.intake or len(self.intake.units) != 2 or not any(k.coronary == 2 for k in self.kinds):
                raise RuntimeError(f"demo folder not read as two patients with a coronary series: {self.v_found.get()}")
            t0 = time.time()
            while time.time() - t0 < 180 and (self.demo_stage is not None or (self.proc and self.proc.running)) and not self._selftest_dialogs:
                self.update()
                time.sleep(0.05)
            if self._selftest_dialogs:
                raise RuntimeError("unexpected dialog: " + model.redact_paths(self._selftest_dialogs[0]))
            out = tmp / "sample" / "anonymised"
            status, _, _ = model.verify_status(out / "_logs")
            if self.demo_stage is not None or status != "PASS":
                raise RuntimeError(f"demo did not complete: stage={self.demo_stage} verify={status} {self.v_progress_text.get()}")
            kept = sum(1 for _ in (out / "DEMO-001").rglob("*.dcm"))
            if kept != 120:
                raise RuntimeError(f"coronary-only demo wrote {kept} files for DEMO-001, expected 120")
            if self.step != DONE_STEP or not self.v_done_head.get().endswith("anonymised and verified"):
                raise RuntimeError(f"the results screen does not show a verified run: step {self.step}, {self.v_done_head.get()!r}")
            if not list((out / "_logs").glob("certificate_*.pdf")):
                raise RuntimeError("no certificate was written for the verified demo output")
            print("selftest: demo folder opened, coronary series anonymised and verified, results screen and certificate shown (PASS)", flush=True)
        finally:
            model.settings_path = real
            shutil.rmtree(tmp, ignore_errors=True)

    def _selftest_viewer_on_synthetic_patients(self) -> None:
        import tempfile
        try:
            from scrubdicom import fixtures
        except ImportError as e:
            print(f"selftest: fixtures unavailable ({e}); viewer data check skipped", flush=True)
            return
        tmp = Path(tempfile.mkdtemp(prefix="scrubdicom_selftest_"))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                fixtures.main(tmp / "fx")
            m = tmp / "patients.csv"
            m.write_text(f"source_folder,study_id\n{tmp / 'fx' / 'ORFAN0231'},SELFTEST-1\n")
            self.v_mode.set("manifest")
            self.v_listway.set(True)
            self._apply_way()
            self.v_manifest.set(str(m))
            self.v_output.set(str(tmp / "out"))
            self.update()
            w = open_viewer(self, "source", "SELFTEST-1")
            if w is None:
                raise RuntimeError("viewer not available")
            t0 = time.time()
            while len(w.series) < 3 and time.time() - t0 < 20:
                self.update()
                time.sleep(0.05)
            if len(w.series) != 3:
                raise RuntimeError(f"viewer loaded {len(w.series)} series, expected 3: {w.v_status.get()}")
            w.tv.selection_set("1")
            self.update()
            if w.view is None or w.photo is None:
                raise RuntimeError("viewer did not render pixels")
            names = {w.th.set(r, "name") for r in w.th.get_children("")}
            if "PatientName" not in names:
                raise RuntimeError("header diff missing")
            w.v_orient.set("Coronal")
            w._orient()
            t0 = time.time()
            while w.vol is None and time.time() - t0 < 20:
                self.update()
                time.sleep(0.05)
            if w.vol is None:
                raise RuntimeError(f"reformat did not load: {w.v_status.get()}")
            w.destroy()
            print(f"selftest: viewer rendered {len(w.series)} series, header diff and coronal reformat on synthetic patients", flush=True)
            self.v_listway.set(False)
            self._apply_way()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def run_app(selftest: bool = False) -> int:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
        except Exception:
            pass
    try:
        app = App(selftest=selftest)
    except tk.TclError as e:
        print(f"Could not open a window: {e}", file=sys.stderr)
        return 2
    app.mainloop()
    return 0
