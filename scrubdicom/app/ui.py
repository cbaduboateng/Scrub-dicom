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
import io
import os
import shlex
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import model
from . import theme
from .model import APP_NAME, APP_VERSION, JobSpec, Settings
from .profile_ui import ProfileEditor
from .runner import EngineProcess
from .viewer import open_viewer

POLL_MS = 250
MONO = ("Menlo", 11) if sys.platform == "darwin" else (("Consolas", 10) if os.name == "nt" else ("TkFixedFont", 10))
LEVEL_MARK = {"ok": "\u2713", "warn": "!", "block": "\u2715"}
MATCH_ON_LABELS = {"patientid": "Patient ID in the scans", "folder": "sub-folder name"}
MATCH_ON_KEYS = {v: k for k, v in MATCH_ON_LABELS.items()}
STEP_TITLES = ("Where are the scans?", "Where should the copies go?", "Review and go")


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
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.minsize(1000, 760)
        geo = self.settings.get("geometry")
        if geo:
            try:
                self.geometry(geo)
            except tk.TclError:
                pass
        self.theme_mode = theme.apply(self, self.settings.get("theme") or None)
        self._make_vars()
        self._make_menu()
        self._make_layout()
        self._apply_mode()
        self._show_step(0)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = self._on_exception   # Tk callbacks: log (paths redacted) and tell the user
        self.v_output.trace_add("write", lambda *_: (self._schedule_output_refresh(), self._suggest_confidential()))
        self.v_manifest.trace_add("write", lambda *_: self._schedule_output_refresh())
        self._refresh_all()
        self._watch_form()
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
        self.v_ready = tk.StringVar(value="")
        self.v_step = tk.StringVar(value="")
        self.v_strip = tk.StringVar(value="Not started")
        self.v_banner = tk.StringVar(value="")

    def _make_menu(self) -> None:
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=False)
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
        r.add_separator()
        r.add_command(label="Viewer: preview original scans...", command=lambda: self._viewer("source"))
        r.add_command(label="Viewer: check anonymised output...", command=lambda: self._viewer("output"))
        r.add_separator()
        r.add_command(label="List patients whose kept slices are too thick", command=lambda: self._start_thick(fix=False))
        r.add_command(label="Remove those patients from the output so they are redone...", command=lambda: self._start_thick(fix=True))
        r.add_separator()
        r.add_command(label="Try it on two sample patients", command=self._run_demo)
        r.add_command(label="Show the command this will run", command=self._show_command)
        m.add_cascade(label="Actions", menu=r)
        h = tk.Menu(m, tearoff=False)
        h.add_command(label="User guide", command=lambda: self.nb.select(self.tab_help))
        h.add_command(label="About", command=lambda: messagebox.showinfo(f"About {APP_NAME}", model.about_text()))
        m.add_cascade(label="Help", menu=h)
        self.config(menu=m)

    def _make_layout(self) -> None:
        head = ttk.Frame(self, padding=(16, 12, 16, 6))
        head.pack(fill="x")
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="Originals are never modified · no network", style="Muted.TLabel").pack(side="left", padx=(14, 0), pady=(8, 0))
        self.b_theme = ttk.Button(head, text="Dark" if self.theme_mode == "light" else "Light", width=6, command=self._toggle_theme)
        self.b_theme.pack(side="right")
        ttk.Label(head, textvariable=self.v_profile_label, style="Muted.TLabel").pack(side="right", padx=(0, 12), pady=(8, 0))
        ttk.Label(head, text="Profile:", style="Muted.TLabel").pack(side="right", padx=(0, 4), pady=(8, 0))
        # the one status strip, on every tab
        self.strip = tk.Label(self, textvariable=self.v_strip, anchor="w", padx=16, pady=8, font=("TkDefaultFont", 13, "bold"))
        self.strip.pack(fill="x", padx=16, pady=(4, 0))
        self.banner = tk.Label(self, textvariable=self.v_banner, anchor="w", padx=16, pady=5, font=("TkDefaultFont", 12))
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(8, 0))
        self.tab_home, self.tab_run, self.tab_series, self.tab_verify, self.tab_share, self.tab_help = (ttk.Frame(self.nb, padding=12) for _ in range(6))
        for tab, name in ((self.tab_home, "Home"), (self.tab_run, "1  Anonymise"), (self.tab_series, "2  Check series"), (self.tab_verify, "3  Verify output"),
                          (self.tab_share, "4  Share safely"), (self.tab_help, "Help")):
            self.nb.add(tab, text=name)
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
        accent = theme.style_or("Accent.TButton")
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
        ttk.Label(head, text="Pseudonymise DICOM studies for blinded research reads.", font=("TkDefaultFont", 22, "bold"), wraplength=COL).pack(anchor="w")
        ttk.Label(head, text="Built and validated on cardiac CT. Every output is checked before it is shared.", font=("TkDefaultFont", 14), style="Muted.TLabel", wraplength=COL).pack(anchor="w", pady=(6, 0))
        tiles = ttk.Frame(box)
        tiles.grid(row=1, column=0, sticky="ew", pady=(36, 0))
        for i in range(3):
            tiles.columnconfigure(i, weight=1, uniform="tile")
        tile, tile_accent = theme.style_or("Tile.TButton"), theme.style_or("Tile.Accent.TButton", accent)
        for i, (text, cmd, style) in enumerate((("Start\nanonymise scans", lambda: self._goto_step(0), tile_accent),
                                                ("Try it\non two sample patients", self._run_demo, tile),
                                                ("Viewer\nscans and headers", lambda: self._viewer("source"), tile))):
            ttk.Button(tiles, text=text, style=style, command=cmd).grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0 if i == 2 else 8), ipady=38)
        self.v_home_recent = tk.StringVar(value="")
        recent = ttk.LabelFrame(box, text="This session", padding=(14, 8, 14, 12))
        recent.grid(row=2, column=0, sticky="ew", pady=(36, 0))
        ttk.Label(recent, textvariable=self.v_home_recent, wraplength=COL - 40, justify="left").pack(anchor="w")
        rb = ttk.Frame(recent)
        rb.pack(anchor="w", pady=(8, 0))
        self.b_home_continue = ttk.Button(rb, text="Continue", command=lambda: self._goto_step(2))
        self.b_home_continue.pack(side="left")
        ttk.Button(rb, text="Share safely", command=lambda: self.nb.select(self.tab_share)).pack(side="left", padx=(8, 0))
        ttk.Button(rb, text="Open output folder", command=lambda: self._open(self._out())).pack(side="left", padx=(8, 0))
        steps = ttk.Frame(box)
        steps.grid(row=4, column=0, sticky="ew", pady=(40, 0))
        for i in range(4):
            steps.columnconfigure(i, weight=1, uniform="step")
        for i, (title, text) in enumerate((("1  Anonymise", "point it at the scans, preview, go"), ("2  Check series", "what was kept, what was dropped"),
                                            ("3  Verify output", "every file re-read for identifiers"), ("4  Share safely", "move the linking logs out, hand over"))):
            f = ttk.Frame(steps)
            f.grid(row=0, column=i, sticky="nw", padx=(0, 12))
            ttk.Label(f, text=title, style="H2.TLabel").pack(anchor="w")
            ttk.Label(f, text=text, style="Muted.TLabel", wraplength=190, justify="left").pack(anchor="w", pady=(2, 0))

    def _refresh_home(self) -> None:
        out = self._out()
        s = self._spec()
        src = Path(s.manifest).name if s.mode == "manifest" and s.manifest else (Path(s.input).name if s.input else "")
        if not out:
            self.v_home_recent.set("No output folder chosen yet. Press Start, or try the demo.")
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
        t.rowconfigure(4, weight=1)
        toggle = theme.style_or("Toggle.TButton")
        switch = theme.style_or("Switch.TCheckbutton")
        accent = theme.style_or("Accent.TButton")
        csv_t = [("CSV files", "*.csv"), ("All files", "*")]

        stack = ttk.Frame(t)
        stack.grid(row=0, column=0, sticky="nsew")
        stack.columnconfigure(0, weight=1)
        self.steps = [ttk.Frame(stack, padding=(8, 8, 8, 4)) for _ in range(3)]
        for s in self.steps:
            s.grid(row=0, column=0, sticky="nsew")
            s.columnconfigure(1, weight=1)

        # ---- step 1: scans
        s1 = self.steps[0]
        ttk.Label(s1, text=STEP_TITLES[0], style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self._step_nav(s1, 0)
        tiles = ttk.Frame(s1)
        tiles.grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self.tiles = {}
        texts = {"manifest": "A list of patients\nCSV: folder, new ID", "single": "One patient\nfolder + new ID", "mapping": "Folder of patients\n+ ID spreadsheet"}
        for mode in model.MODES:
            rb = ttk.Radiobutton(tiles, text=texts[mode], value=mode, variable=self.v_mode, command=self._apply_mode, style=toggle, width=22)
            rb.pack(side="left", padx=(0, 10), ipady=14)
            self.tiles[mode] = rb
        self.lbl_mode = ttk.Label(s1, text="", style="Muted.TLabel", wraplength=860, justify="left")
        self.lbl_mode.grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.rows_manifest = self._row(s1, 3, "Patient list", self.v_manifest, "file", "A CSV with two columns: source_folder (the folder holding that patient's scans) and study_id (the new ID). One patient per row.", csv_t)
        self.rows_input = self._row(s1, 4, "Scans folder", self.v_input, "dir", "All sub-folders are searched.")
        self.rows_study_id = self._row(s1, 5, "New ID", self.v_study_id, None, "Applied to every file in the folder. Letters, digits, spaces, - _ . (spaces become _ in folder names).", width=24)
        self.rows_mapping = self._row(s1, 6, "ID spreadsheet", self.v_mapping, "file", "CSV or Excel with an old-ID column and a new-ID column. Column names are auto-detected; see the columns row if not.",
                                      [("Spreadsheets", "*.csv *.xlsx *.xlsm"), ("All files", "*")])
        adv = ttk.Frame(s1)
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
        ttk.Label(cols, text="old ID is the").grid(row=0, column=6, sticky="w", padx=(12, 4))
        ttk.Combobox(cols, textvariable=self.v_match_on_label, values=tuple(MATCH_ON_LABELS.values()), state="readonly", width=19).grid(row=0, column=7, sticky="w")
        ttk.Checkbutton(s1, text="More ways and options", variable=self.v_more, command=self._apply_mode, style=toggle).grid(row=8, column=0, columnspan=4, sticky="w", pady=(10, 0))

        # ---- step 2: output
        s2 = self.steps[1]
        ttk.Label(s2, text=STEP_TITLES[1], style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self._step_nav(s2, 1)
        self._row(s2, 1, "Output folder", self.v_output, "dir", "One folder per patient plus _logs and _review will be created here. Only anonymised files and non-confidential logs ever go here.")
        ttk.Button(s2, text="Open", command=lambda: self._open(self._out())).grid(row=1, column=4, padx=(8, 0))
        ttk.Label(s2, textvariable=self.v_out_status, style="Muted.TLabel").grid(row=2, column=0, columnspan=5, sticky="w")
        self._row(s2, 3, "Confidential folder", self.v_confidential, "dir", "Where the linkage log (study ID -> patient), the UID salt and the run logs go. Must be outside the output folder; ideally a different, encrypted drive. Nothing in the output folder can then re-identify a patient.")
        ttk.Button(s2, text="Suggest", command=lambda: self.v_confidential.set(model.suggest_confidential(self.v_output.get()))).grid(row=3, column=4, padx=(8, 0))
        ttk.Label(s2, text="The output folder can be handed over; the confidential folder never leaves you.", style="Muted.TLabel").grid(row=4, column=0, columnspan=5, sticky="w")
        self.lbl_drive = ttk.Label(s2, textvariable=self.v_drive_note, style="Warn.TLabel", wraplength=860, justify="left")
        self.lbl_drive.grid(row=5, column=0, columnspan=5, sticky="w", pady=(8, 0))

        # ---- step 3: review and go
        s3 = self.steps[2]
        ttk.Label(s3, text=STEP_TITLES[2], style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self._step_nav(s3, 2)
        ttk.Label(s3, text="Profile").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=6)
        prow = ttk.Frame(s3)
        prow.grid(row=1, column=1, columnspan=3, sticky="w", pady=6)
        self.cb_profile = ttk.Combobox(prow, textvariable=self.v_profile_label, state="readonly", width=40)
        self.cb_profile.pack(side="left")
        self.cb_profile.bind("<<ComboboxSelected>>", lambda _e: self._profile_chosen())
        ttk.Button(prow, text="Edit...", command=self._edit_profiles).pack(side="left", padx=(8, 0))
        help_mark(prow, "What to keep beyond the default. 'Blinded read' removes everything identifying. Other profiles may keep sex, a 5-year age bucket, shifted dates or the scanner model, within what DICOM PS3.15 allows.").pack(side="left", padx=(8, 0))
        sw = ttk.Frame(s3)
        sw.grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.cb_ctca = ttk.Checkbutton(sw, text="Coronary series only", variable=self.v_ctca, style=switch)
        self.cb_ctca.pack(side="left", padx=(0, 24))
        self.cb_resume = ttk.Checkbutton(sw, text="Skip patients already done", variable=self.v_resume, style=switch)
        self.cb_resume.pack(side="left", padx=(0, 24))
        ttk.Checkbutton(sw, text="Check output when done", variable=self.v_verify_after, style=switch).pack(side="left", padx=(0, 24))
        self.lbl_manifest_only = ttk.Label(sw, text="", style="Muted.TLabel")
        self.lbl_manifest_only.pack(side="left")
        adv3 = ttk.Frame(s3)
        adv3.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.adv3 = adv3
        ttk.Checkbutton(adv3, text="Keep scanner technical details", variable=self.v_keep_tech, style=switch).pack(side="left", padx=(0, 24))
        ttk.Checkbutton(adv3, text="No series sub-folders", variable=self.v_flat, style=switch).pack(side="left")
        help_mark(adv3, "Keep scanner technical details: kernel and scan options; off for a blinded read.\nNo series sub-folders: one flat folder per patient.\nCheck output when done reads headers only and is safe on any cohort size; allow a few minutes per 100 patients.").pack(side="left", padx=(8, 0))
        self.lbl_summary = ttk.Label(s3, textvariable=self.v_summary, wraplength=880, justify="left", font=MONO)
        self.lbl_summary.grid(row=4, column=0, columnspan=4, sticky="w", pady=(16, 8))
        act = ttk.Frame(s3)
        act.grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.b_dry = ttk.Button(act, text="Preview", width=14, command=lambda: self._start_run(dry_run=True))
        self.b_start = ttk.Button(act, text="Anonymise", width=16, style=accent, command=self._start_run)
        self.b_stop = ttk.Button(act, text="Stop", width=8, command=self._stop, state="disabled")
        self.b_dry.pack(side="left", ipady=6)
        self.b_start.pack(side="left", padx=(10, 0), ipady=6)
        self.b_stop.pack(side="left", padx=(10, 0), ipady=6)
        self.b_viewer = ttk.Button(act, text="Open viewer", command=lambda: self._viewer("source"))
        self.b_viewer.pack(side="left", padx=(28, 0), ipady=6)
        Tooltip(self.b_start, "Runs Preview first if you have not, so a wrong folder is caught before anything is written.")
        self.lbl_ready = ttk.Label(s3, textvariable=self.v_ready, wraplength=880, justify="left")
        self.lbl_ready.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))

        # ---- navigation
        nav = ttk.Frame(t, padding=(8, 4))
        nav.grid(row=1, column=0, sticky="ew")
        self.b_back = ttk.Button(nav, text="Back", width=8, command=lambda: self._show_step(self.step - 1))
        self.b_back.pack(side="left")
        self.b_next = ttk.Button(nav, text="Next", width=10, style=accent, command=lambda: self._show_step(self.step + 1))
        self.b_next.pack(side="left", padx=(8, 0))
        ttk.Label(nav, textvariable=self.v_step, style="Muted.TLabel").pack(side="left", padx=(16, 0))
        self.v_nav_reason = tk.StringVar(value="")
        ttk.Label(nav, textvariable=self.v_nav_reason, style="Warn.TLabel", wraplength=520, justify="left").pack(side="left", padx=(16, 0))
        more = ttk.Menubutton(nav, text="More")
        mm = tk.Menu(more, tearoff=False)
        mm.add_command(label="Show the command this will run", command=self._show_command)
        mm.add_command(label="Open output folder", command=lambda: self._open(self._out()))
        mm.add_command(label="Open log file", command=lambda: self._open(self.log_path))
        mm.add_command(label="Copy log", command=self._copy_log)
        mm.add_command(label="Try it on two sample patients", command=self._run_demo)
        mm.add_separator()
        mm.add_checkbutton(label="Show patients skipped as already done", variable=self.v_show_all)
        more["menu"] = mm
        more.pack(side="right")

        # ---- recovery card (hidden until needed)
        self.card = ttk.LabelFrame(t, text="What happened", padding=(14, 8, 14, 12))
        self.card.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.card.columnconfigure(0, weight=1)
        self.v_card_title, self.v_card_text = tk.StringVar(), tk.StringVar()
        ttk.Label(self.card, textvariable=self.v_card_title, style="H2.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self.card, textvariable=self.v_card_text, wraplength=800, justify="left").grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.b_card = ttk.Button(self.card, text="", style=accent)
        self.b_card.grid(row=0, column=1, rowspan=2, padx=(16, 0))
        ttk.Button(self.card, text="Dismiss", command=self._hide_card).grid(row=0, column=2, rowspan=2, padx=(8, 0))
        self.card.grid_remove()

        # ---- progress and activity
        prog = ttk.Frame(t, padding=(8, 0))
        prog.grid(row=3, column=0, sticky="ew", pady=(6, 4))
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, variable=self.v_progress, maximum=100)
        self.bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(prog, textvariable=self.v_progress_text, style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
        logf = ttk.Frame(t, padding=(8, 0))
        logf.grid(row=4, column=0, sticky="nsew")
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log = tk.Text(logf, font=MONO, wrap="none", state="disabled", height=4, width=60, undo=False)
        ys = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ys.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        self._profile_entries = []
        self._refresh_profile_label()

    def _step_nav(self, frame, index: int) -> None:
        """Back / Next beside the step title, so navigation is in view without scrolling to the bottom bar."""
        nav = ttk.Frame(frame)
        nav.grid(row=0, column=3, columnspan=2, sticky="e", pady=(0, 10))
        b_back = ttk.Button(nav, text="\u2039 Back", width=8, command=lambda: self._show_step(index - 1))
        b_back.pack(side="left")
        if index == 0:
            b_back.state(["disabled"])
        if index < 2:
            b_next = ttk.Button(nav, text="Next \u203a", width=8, style=theme.style_or("Accent.TButton"), command=lambda: self._show_step(index + 1))
            b_next.pack(side="left", padx=(6, 0))
            self._top_next = getattr(self, "_top_next", {})
            self._top_next[index] = b_next

    def _apply_mode(self) -> None:
        mode = self.v_mode.get()
        more = self.v_more.get()
        if mode == "mapping" and not more:
            self.v_more.set(True)
            more = True
        if more:
            self.tiles["mapping"].pack(side="left", padx=(0, 10), ipady=14)
        else:
            self.tiles["mapping"].pack_forget()
        groups = {"manifest": self.rows_manifest, "single": self.rows_input + self.rows_study_id, "mapping": self.rows_input + self.rows_mapping}
        adv_groups = {"manifest": self.rows_remap + self.rows_picks, "single": [], "mapping": self.rows_cols}
        every = set(sum(groups.values(), []) + sum(adv_groups.values(), []))
        show = set(groups.get(mode, [])) | (set(adv_groups.get(mode, [])) if more else set())
        for w in every:
            (w.grid if w in show else w.grid_remove)()
        (self.adv1.grid if more and adv_groups.get(mode) else self.adv1.grid_remove)()
        (self.adv3.grid if more else self.adv3.grid_remove)()
        manifest = mode == "manifest"
        for cb in (self.cb_ctca, self.cb_resume):
            cb.state(["!disabled"] if manifest else ["disabled"])
        self.lbl_manifest_only.configure(text="" if manifest else "(patient-list runs only)")
        self.lbl_mode.configure(text=model.MODE_HELP.get(mode, ""))
        self._schedule_validate()

    def _show_step(self, i: int) -> None:
        self.step = max(0, min(2, i))
        for k, s in enumerate(self.steps):
            if k == self.step:
                s.tkraise()
            else:
                s.lower()
        self.v_step.set(f"Step {self.step + 1} of 3: {STEP_TITLES[self.step]}")
        self.b_back.state(["!disabled"] if self.step > 0 else ["disabled"])
        if self.step == 2:
            self.b_next.pack_forget()
        elif not self.b_next.winfo_ismapped():
            self.b_next.pack(side="left", padx=(8, 0), after=self.b_back)
        self._live_validate()

    # ------------------------------------------------------------------ tabs 2-4 with empty states
    def _tree(self, container, columns: list[tuple[str, str, int]], height=12) -> ttk.Treeview:
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
        self.empty_series = self._empty(c, "No series decisions yet.\nRun Preview with 'Coronary series only' on to see what will be kept.", "Go to Preview", lambda: self._goto_step(2))
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
        self.empty_verify = self._empty(c, "Nothing to check yet.\nAnonymise some patients first; the check runs by itself when the run finishes.", "Go to Anonymise", lambda: self._goto_step(2))

    def _build_share_tab(self) -> None:
        t = self.tab_share
        ttk.Label(t, text="Is the output folder safe to hand over?", style="H2.TLabel").pack(anchor="w")
        ttk.Label(t, text="The whole _logs folder is confidential. Move it out before the output leaves this computer.", style="Muted.TLabel").pack(anchor="w", pady=(2, 6))
        c = self._container(t)
        self.tv_checks = self._tree(c, [("mark", "", 28), ("title", "Check", 300), ("detail", "Detail", 500)], height=7)
        self.empty_share = self._empty(c, "Nothing to share yet.\nAnonymise some patients first.", "Go to Anonymise", lambda: self._goto_step(2))
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
        t = self.tab_help
        top = ttk.Frame(t)
        top.pack(fill="x", pady=(0, 8))
        ttk.Button(top, text="Try it on two sample patients", style=theme.style_or("Accent.TButton"), command=self._run_demo).pack(side="left", ipady=4)
        ttk.Label(top, text="Runs the whole flow on built-in synthetic scans in about twenty seconds. No real data involved.", style="Muted.TLabel").pack(side="left", padx=(12, 0))
        txt = tk.Text(t, font=MONO, wrap="word", state="normal")
        self.txt_help = txt
        ys = ttk.Scrollbar(t, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=ys.set)
        try:
            body = (Path(__file__).with_name("HELP.txt")).read_text(encoding="utf-8")
        except OSError:
            body = "See README.md"
        txt.insert("1.0", body + "\n\n" + model.about_text())
        txt.configure(state="disabled")
        txt.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")

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
        return JobSpec(mode=self.v_mode.get(), output=self.v_output.get(), manifest=self.v_manifest.get(), remap=self.v_remap.get(),
                       input=self.v_input.get(), study_id=self.v_study_id.get(), mapping=self.v_mapping.get(),
                       current_col=self.v_current_col.get(), new_col=self.v_new_col.get(), sheet=self.v_sheet.get(),
                       match_on=self.v_match_on.get(), series_pick=self.v_series_pick.get(), ctca_only=self.v_ctca.get(),
                       resume=self.v_resume.get(), keep_technical=self.v_keep_tech.get(), flat=self.v_flat.get(), dry_run=dry_run,
                       profile=self.v_profile.get(), confidential=self.v_confidential.get(), series_select=self.v_series_select.get())

    def _spec_key(self) -> tuple:
        s = self._spec()
        return (s.mode, s.manifest, s.input, s.study_id, s.mapping, s.output, s.confidential, s.profile, s.ctca_only, s.series_pick, s.series_select, s.match_on, s.current_col, s.new_col)

    def _save_settings(self) -> None:
        model.spec_to_settings(self._spec(), self.settings)
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
        for b in (self.b_dry, self.b_start, self.b_verify, self.b_share_check):
            b.state(["disabled"] if running else ["!disabled"])
        self.b_stop.state(["!disabled"] if running else ["disabled"])
        if not running:
            self._live_validate()

    # ================================================================== theme, validation, status
    def _apply_theme_colours(self) -> None:
        pal = theme.palette()
        for w in (self.log, self.txt_verify, self.txt_help):
            theme.style_text(w)
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

    def _schedule_validate(self) -> None:
        if self._validate_id:
            self.after_cancel(self._validate_id)
        self._validate_id = self.after(200, self._live_validate)

    def _live_validate(self) -> None:
        self._validate_id = None
        if not hasattr(self, "b_next"):
            return
        problems = self._spec().validate()
        step1 = [p for p in problems if "output" not in p.lower() and "profile" not in p.lower() and "confidential" not in p.lower()]
        step2 = [p for p in problems if "output" in p.lower() or "confidential" in p.lower()]
        running = bool(self.proc and self.proc.running)
        ok_here = {0: not step1, 1: not step2, 2: not problems}[self.step]
        self.b_next.state(["!disabled"] if ok_here and self.step < 2 else ["disabled"])
        top = getattr(self, "_top_next", {}).get(self.step)
        if top is not None:
            top.state(["!disabled"] if ok_here else ["disabled"])
        here = {0: step1, 1: step2, 2: problems}[self.step]
        self.v_nav_reason.set(("Next needs: " + here[0]) if here and self.step < 2 else "")
        previewed = self.previewed_key == self._spec_key()
        if problems:
            self.v_ready.set("Before you can start: " + "  ·  ".join(problems[:3]))
            self.lbl_ready.configure(style="Warn.TLabel")
        elif not previewed:
            self.v_ready.set("Run Preview first. It writes nothing and shows what would happen.")
            self.lbl_ready.configure(style="Muted.TLabel")
        else:
            self.v_ready.set("Previewed. Anonymise when you are happy with what you saw.")
            self.lbl_ready.configure(style="Ok.TLabel")
        if not running:
            self.b_dry.state(["!disabled"] if not problems else ["disabled"])
            self.b_start.state(["!disabled"] if not problems and previewed else ["disabled"])
        self.v_summary.set(self._summary_text(problems))
        self.v_drive_note.set(self._drive_note())
        self._update_banner()

    def _summary_text(self, problems: list[str]) -> str:
        s = self._spec()
        if problems:
            return "Complete the earlier steps to see the summary."
        prof = model.profile_for_path(s.profile)
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
        lines = [f"Anonymise {who}",
                 f"Output:        {s.output}",
                 f"Confidential:  {s.confidential}",
                 f"Profile:       {prof.name}" + (f"   ·   {', '.join(opts)}" if opts else ""),
                 "Originals are not modified."]
        return "\n".join(lines)

    def _drive_note(self) -> str:
        s = self._spec()
        src = s.manifest if s.mode == "manifest" else s.input
        if not s.output.strip() or not src.strip():
            return ""
        try:
            a, b = Path(s.output).expanduser().resolve(), Path(src).expanduser().resolve()
        except OSError:
            return ""
        key = lambda p: p.parts[:3] if len(p.parts) >= 3 and p.parts[1] == "Volumes" else (p.anchor,)
        if key(a) == key(b):
            return "The output is on the same drive as the scans. That works, but a separate drive keeps the anonymised copies apart from the originals."
        return ""

    def _update_banner(self) -> None:
        if not hasattr(self, "banner"):
            return
        prof = model.profile_for_path(self.v_profile.get())
        kept = prof.kept_summary()
        pal = theme.palette()
        if kept:
            self.v_banner.set(f"This profile retains: {', '.join(kept)}. The output is not fully blinded.")
            self.banner.configure(bg=pal["warn"], fg="#1f2328")
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
                    text, level = "Verified. Read the warnings on Share safely before handing over.", "warn"
                else:
                    text, level = "Verified. Safe to hand over.", "ok"
        colours = {"muted": (pal["border"], pal["text"]), "info": (pal["accent"], "#ffffff"), "ok": (pal["ok"], "#ffffff"),
                   "warn": (pal["warn"], "#1f2328"), "error": (pal["error"], "#ffffff")}
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
            logs = self._logs()
            if logs:
                self.log_path = logs / f"app_{log_name}_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        self._clear_log()
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

    def _start_run(self, dry_run: bool = False) -> None:
        spec = self._spec(dry_run)
        problems = spec.validate()
        if problems:
            messagebox.showerror("Cannot start", "\n".join(problems))
            return
        if not dry_run and self.previewed_key != self._spec_key():
            messagebox.showinfo("Preview first", "Run Preview first. It writes nothing and shows what would happen, so a wrong folder is caught before anything is written.")
            return
        if not dry_run and self.demo_stage is None:
            if not messagebox.askokcancel("Anonymise", self.v_summary.get() + "\n\nStart?  (More > Show the command for the exact command line.)"):
                return
        self._start_job("dry" if dry_run else "run", ["run", *spec.run_args()], None if dry_run else "run", "Preview" if dry_run else "Anonymisation")

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
            self.nb.select(self.tab_share)
            if rc == 0:
                self._show_card("Ready to hand over", "Every output file is byte-for-byte as it was when verified. The attestation and checksum manifest in _logs travel with it.",
                                "Open output folder", lambda: self._open(self._out()))
            else:
                self._show_card("Do not hand over", "Files changed, went missing or were added since verification. Run the check again, then hand over.",
                                "Check now", self._start_verify)
        if job == "run" and rc == 0 and self.v_verify_after.get():
            self.after(400, self._start_verify)
        elif job == "verify" and self.demo_stage != "verify":
            self.nb.select(self.tab_verify)
        if self.demo_stage == "preview" and job == "dry" and rc == 0:
            self.demo_stage = "run"
            self.after(600, self._start_run)
        elif self.demo_stage == "run" and job == "run" and rc == 0:
            self.demo_stage = "verify"
        elif self.demo_stage == "verify" and job == "verify":
            self.demo_stage = None
            self.nb.select(self.tab_verify)
            self._show_card("Demo complete", "Two sample patients were previewed, anonymised and checked. Look at 'Check series', open the viewer, then try your own list.",
                            "Open viewer", lambda: self._viewer("output"))
        elif self.demo_stage and (rc != 0 or stopped):
            self.demo_stage = None

    def _recovery(self, job: str | None, rc: int | None, stopped: bool, log_text: str) -> None:
        if rc == 0 or stopped:
            if job == "verify" and rc == 0:
                self._hide_card()
            return
        if "no longer reachable" in log_text or "drive disappeared" in log_text or "Could not write the completion marker" in log_text:
            self._show_card("The output drive disconnected", "Completed patients are safe. Reconnect the drive, then press Resume: the run continues where it stopped.",
                            "Resume", lambda: (self.v_resume.set(True), self._start_run()))
        elif job == "verify":
            self._show_card("Identifiers were found in the output", "Do not share it. The report names each file and tag. If it is a word you typed that is also a common word, refine it; if it is a real leak, report it.",
                            "Open report", lambda: self.nb.select(self.tab_verify))
        elif "folder not found" in log_text and "Done in" in log_text:
            self._show_card("Some folders in the list were not found", "The run finished, but the patients whose folders are missing were skipped. Check the paths in the list (or the drive path fix) and run again with 'Skip patients already done'.",
                            "Go to step 1", lambda: self._goto_step(0))
        else:
            self._show_card(f"{'Preview' if job == 'dry' else 'The run'} did not finish", "The last lines of the activity log say why. Copy them if you need to ask for help.",
                            "Copy log", self._copy_log)

    def _show_card(self, title: str, text: str, button: str, command) -> None:
        self.v_card_title.set(title)
        self.v_card_text.set(text)
        self.b_card.configure(text=button, command=command)
        self.card.grid()

    def _hide_card(self) -> None:
        self.card.grid_remove()

    def _poll(self) -> None:
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
        if total and exp:
            pct = ((n - 1) + min(files, exp) / exp) / total * 100
            self._bar_mode(True)
            self.v_progress.set(pct)
            text = f"{verb} {where}, {files:,} of {exp:,} files"
        elif exp:
            self._bar_mode(True)
            self.v_progress.set(min(files, exp) / exp * 100)
            text = f"{verb} {where}, {files:,} of {exp:,} files"
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
        problems = spec.validate()
        text = shown_command(spec.command())
        if problems:
            text += "\n\nNot runnable yet:\n- " + "\n- ".join(problems)
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
            self.v_status.set(self.v_profile_desc.get())
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
        preview, so land on the Anonymise step with the button enabled."""
        self.previewed_key = self._spec_key()
        self._goto_step(2)
        self._live_validate()
        self.v_ready.set(f"{n} ticked series saved for {study_id}. Press Anonymise.")
        self.lbl_ready.configure(style="Ok.TLabel")
        self.lift()
        self.b_start.focus_set()

    def _viewer(self, mode: str, study_id: str | None = None) -> None:
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
        s = f"Patients done: {len(done)}   ·   half-finished (will be redone): {len(partial)}"
        if n is not None:
            s += f"   ·   in the list: {n}"
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
    def _run_demo(self) -> None:
        if self._busy():
            return
        try:
            from scrubdicom import fixtures
        except ImportError as e:
            messagebox.showerror(APP_NAME, f"Sample data needs numpy, which is not installed: {e}")
            return
        base = model.settings_path().parent / "sample"
        try:
            import shutil
            shutil.rmtree(base, ignore_errors=True)      # a fresh demo every time: synthetic data only, nothing to keep
            with contextlib.redirect_stdout(io.StringIO()):
                fixtures.main(base / "scans")
            m = base / "patients.csv"
            m.write_text(f"source_folder,study_id\n{base / 'scans' / 'ORFAN0231'},DEMO-001\n{base / 'scans' / 'ORFAN0418'},DEMO-002\n")
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Could not create the sample data: {e}")
            return
        self.v_mode.set("manifest")
        self.v_manifest.set(str(m))
        self.v_output.set(str(base / "anonymised"))
        self.v_confidential.set(str(base / "confidential"))
        self.v_remap.set("")
        self.v_series_pick.set("")
        self.v_profile.set("")
        self.v_ctca.set(False)          # the synthetic series are tiny; keep everything so there is output to look at
        self.v_resume.set(True)
        self.v_verify_after.set(True)
        self._refresh_profile_label()
        self._apply_mode()
        self.previewed_key = None
        self.demo_stage = "preview"
        self._goto_step(2)
        self._live_validate()
        self.after(300, lambda: self._start_run(dry_run=True))

    # ================================================================== lifecycle
    def _on_close(self) -> None:
        if self.proc and self.proc.running:
            if not messagebox.askyesno("Quit?", "A job is running. Stop it and quit?"):
                return
            self.proc.stop()
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
        for tab in (self.tab_home, self.tab_run, self.tab_series, self.tab_verify, self.tab_share, self.tab_help):
            self.nb.select(tab)
            self.update()
        for mode in model.MODES:
            self.v_mode.set(mode)
            self._apply_mode()
            for i in range(3):
                self._show_step(i)
                self.update()
        self.v_mode.set("manifest")
        self._apply_mode()
        self.nb.select(self.tab_run)
        self._show_step(0)
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
        self._selftest_viewer_on_synthetic_patients()
        self._selftest_demo()
        self.after(200, self.destroy)

    def _selftest_demo(self) -> None:
        """Run the built-in demo end to end (preview, anonymise, check) inside the self-test, so a frozen build proves
        the whole flow. Sample data goes to a temporary folder, not the user's settings folder."""
        import shutil
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="scrubdicom_demo_"))
        real = model.settings_path
        model.settings_path = lambda: tmp / "settings.json"   # redirect the demo's sample folder
        try:
            self._run_demo()
            t0 = time.time()
            while time.time() - t0 < 180 and (self.demo_stage is not None or (self.proc and self.proc.running)):
                self.update()
                time.sleep(0.05)
            out = tmp / "sample" / "anonymised"
            status, _, _ = model.verify_status(out / "_logs")
            if self.demo_stage is not None or status != "PASS":
                raise RuntimeError(f"demo did not complete: stage={self.demo_stage} verify={status} {self.v_progress_text.get()}")
            print("selftest: demo flow previewed, anonymised and verified two sample patients (PASS)", flush=True)
        finally:
            model.settings_path = real
            shutil.rmtree(tmp, ignore_errors=True)

    def _selftest_viewer_on_synthetic_patients(self) -> None:
        import shutil
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
            self._apply_mode()
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
