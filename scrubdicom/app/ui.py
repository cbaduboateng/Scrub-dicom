"""The Scrub-DICOM window. Tkinter/ttk only: no web server, no port, no third-party UI dependency.

Layout: five tabs (Run, Series decisions, Verify, Logs & sharing, Help) over a status bar. All engine work
happens in a child process (see runner.py) and the window polls it four times a second. Anything that is
logic rather than widgets lives in model.py and is tested there.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import model
from .model import APP_NAME, APP_VERSION, JobSpec, Settings
from .runner import EngineProcess
from .viewer import open_viewer
from .profile_ui import ProfileEditor
from . import theme

POLL_MS = 250
MONO = ("Menlo", 11) if sys.platform == "darwin" else (("Consolas", 10) if os.name == "nt" else ("TkFixedFont", 10))
COLOURS = {"error": "#b42318", "warn": "#9a6700", "ok": "#1a7f37", "plain": None, "keep": "#1a7f37", "drop": "#6e7781", "check": "#9a6700", "block": "#b42318"}
LEVEL_MARK = {"ok": "\u2713", "warn": "!", "block": "\u2715"}
MATCH_ON_LABELS = {"patientid": "Patient ID in the scans", "folder": "sub-folder name"}
MATCH_ON_KEYS = {v: k for k, v in MATCH_ON_LABELS.items()}


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
        lbl = tk.Label(self._win, text=self.text, justify="left", wraplength=420, bg=pal["panel"], fg=pal["text"],
                       relief="solid", borderwidth=1, padx=10, pady=8, font=("TkDefaultFont", 11))
        lbl.pack()

    def _hide(self, _e=None) -> None:
        if self._id:
            self.widget.after_cancel(self._id)
            self._id = None
        if self._win:
            self._win.destroy()
            self._win = None


def shown_command(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


class App(tk.Tk):
    def __init__(self, selftest: bool = False):
        super().__init__()
        self.selftest = selftest
        self.settings = Settings.load()
        self.proc: EngineProcess | None = None
        self.job: str | None = None
        self.log_path: Path | None = None
        self.skipped_hidden = 0
        self._out_refresh_id: str | None = None
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
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.v_output.trace_add("write", lambda *_: self._schedule_output_refresh())
        self.v_manifest.trace_add("write", lambda *_: self._schedule_output_refresh())
        self._refresh_all()
        self.after(POLL_MS, self._poll)
        if selftest:
            self.after(500, self._selftest_walk)

    # ================================================================== construction
    def _make_vars(self) -> None:
        s = self.settings
        sv = lambda k: tk.StringVar(value=str(s.get(k) or ""))
        bv = lambda k: tk.BooleanVar(value=bool(s.get(k)))
        self.v_mode = sv("mode")
        self.v_output, self.v_manifest, self.v_remap, self.v_input = sv("output"), sv("manifest"), sv("remap"), sv("input")
        self.v_study_id = tk.StringVar()
        self.v_mapping, self.v_current_col, self.v_new_col, self.v_sheet = sv("mapping"), sv("current_col"), sv("new_col"), sv("sheet")
        self.v_match_on = tk.StringVar(value=s.get("match_on") or "patientid")
        self.v_match_on_label = tk.StringVar(value=MATCH_ON_LABELS.get(self.v_match_on.get(), MATCH_ON_LABELS["patientid"]))
        self.v_match_on_label.trace_add("write", lambda *_: self.v_match_on.set(MATCH_ON_KEYS.get(self.v_match_on_label.get(), "patientid")))
        self.v_series_pick = sv("series_pick")
        self.v_profile = sv("profile")
        self.v_profile_desc = tk.StringVar()
        self.v_profile_label = tk.StringVar()
        self.v_ctca, self.v_resume, self.v_keep_tech, self.v_flat = bv("ctca_only"), bv("resume"), bv("keep_technical"), bv("flat")
        self.v_verify_after, self.v_show_all = bv("verify_after_run"), bv("show_all_lines")
        self.v_needles = tk.StringVar()              # never persisted: these are identifiers
        self.v_series_filter = tk.StringVar(value="All")
        self.v_series_query = tk.StringVar()
        self.v_progress = tk.DoubleVar(value=0.0)
        self.v_progress_text = tk.StringVar(value="Idle")
        self.v_out_status = tk.StringVar(value="")
        self.v_status = tk.StringVar(value="")

    def _make_menu(self) -> None:
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=False)
        f.add_command(label="Open output folder", command=lambda: self._open(self._out()))
        f.add_command(label="Open _logs folder", command=lambda: self._open(self._out() / "_logs" if self._out() else None))
        f.add_separator()
        f.add_command(label="Quit", command=self._on_close, accelerator="Cmd+Q" if sys.platform == "darwin" else "Alt+F4")
        m.add_cascade(label="File", menu=f)
        r = tk.Menu(m, tearoff=False)
        r.add_command(label="Preview (writes nothing)", command=lambda: self._start_run(dry_run=True))
        r.add_command(label="Anonymise", command=self._start_run)
        r.add_command(label="Stop", command=self._stop)
        r.add_command(label="Verify the output", command=self._start_verify)
        r.add_separator()
        r.add_command(label="List patients whose kept slices are too thick", command=lambda: self._start_thick(fix=False))
        r.add_command(label="Remove those patients from the output so they are redone...", command=lambda: self._start_thick(fix=True))
        r.add_separator()
        r.add_command(label="Viewer: preview original scans...", command=lambda: self._viewer("source"))
        r.add_command(label="Viewer: check anonymised output...", command=lambda: self._viewer("output"))
        r.add_separator()
        r.add_command(label="Show the command this will run", command=self._show_command)
        m.add_cascade(label="Actions", menu=r)
        h = tk.Menu(m, tearoff=False)
        h.add_command(label="User guide", command=lambda: self.nb.select(self.tab_help))
        h.add_command(label="About", command=lambda: messagebox.showinfo(f"About {APP_NAME}", model.about_text()))
        m.add_cascade(label="Help", menu=h)
        self.config(menu=m)

    def _make_layout(self) -> None:
        head = ttk.Frame(self, padding=(14, 10, 14, 6))
        head.pack(fill="x")
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="Originals are never modified · no network", style="Muted.TLabel").pack(side="left", padx=(14, 0), pady=(6, 0))
        self.b_theme = ttk.Button(head, text="Dark" if self.theme_mode == "light" else "Light", width=6, command=self._toggle_theme)
        self.b_theme.pack(side="right")
        ttk.Label(head, textvariable=self.v_profile_label, style="Muted.TLabel").pack(side="right", padx=(0, 12), pady=(6, 0))
        ttk.Label(head, text="Profile:", style="Muted.TLabel").pack(side="right", padx=(0, 4), pady=(6, 0))
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(4, 0))
        self.tab_run, self.tab_series, self.tab_verify, self.tab_share, self.tab_help = (ttk.Frame(self.nb, padding=10) for _ in range(5))
        for tab, name in ((self.tab_run, "1  Anonymise"), (self.tab_series, "2  Check series"), (self.tab_verify, "3  Verify output"),
                          (self.tab_share, "4  Share safely"), (self.tab_help, "Help")):
            self.nb.add(tab, text=name)
        self._build_run_tab()
        self._build_series_tab()
        self._build_verify_tab()
        self._build_share_tab()
        self._build_help_tab()
        bar = ttk.Frame(self, padding=(10, 4))
        bar.pack(fill="x")
        ttk.Label(bar, textvariable=self.v_status).pack(side="left")
        ttk.Label(bar, text=f"Engine {model.ENGINE_VERSION} · {'packaged' if model.is_frozen() else 'source'} · no network access",
                  style="Muted.TLabel").pack(side="right")
        self._apply_theme_colours()
        self._watch_form()

    # ------------------------------------------------------------------ Run tab
    def _row(self, parent, r, label, var, kind=None, tip=None, filetypes=None, width=None):
        """One labelled entry with a Choose button and an optional '?' tooltip. Returns the widgets so a mode or the
        Advanced toggle can hide them."""
        lbl = ttk.Label(parent, text=label)
        lbl.grid(row=r, column=0, sticky="w", padx=(0, 12), pady=6)
        e = ttk.Entry(parent, textvariable=var, width=width or 40)
        e.grid(row=r, column=1, sticky="ew", pady=6)
        widgets = [lbl, e]
        if kind:
            b = ttk.Button(parent, text="Choose...", command=lambda: self._browse(var, kind, filetypes))
            b.grid(row=r, column=2, padx=(8, 0), pady=6)
            widgets.append(b)
        if tip:
            q = ttk.Label(parent, text="?", style="Muted.TLabel", cursor="question_arrow")
            q.grid(row=r, column=3, padx=(8, 0))
            Tooltip(q, tip)
            widgets.append(q)
        return widgets

    def _card(self, parent, row, title):
        f = ttk.LabelFrame(parent, text=title, padding=(14, 8, 14, 12))
        f.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        f.columnconfigure(1, weight=1)
        return f

    def _build_run_tab(self) -> None:
        t = self.tab_run
        t.columnconfigure(0, weight=1)
        t.rowconfigure(6, weight=1)
        toggle = theme.style_or("Toggle.TButton")
        switch = theme.style_or("Switch.TCheckbutton")
        accent = theme.style_or("Accent.TButton")
        csv_t = [("CSV files", "*.csv"), ("All files", "*")]
        self.v_advanced = tk.BooleanVar(value=False)

        # ---- 1 scans
        c1 = self._card(t, 0, "1   Scans")
        seg = ttk.Frame(c1)
        seg.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        for mode in model.MODES:
            ttk.Radiobutton(seg, text=model.MODE_LABELS[mode], value=mode, variable=self.v_mode, command=self._apply_mode, style=toggle).pack(side="left", padx=(0, 8))
        self.lbl_mode = ttk.Label(c1, text="", style="Muted.TLabel", wraplength=860, justify="left")
        self.lbl_mode.grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 4))
        self.rows_manifest = self._row(c1, 2, "Patient list", self.v_manifest, "file", "A CSV with two columns: source_folder (the folder holding that patient's scans) and study_id (the new ID). One patient per row.", csv_t)
        self.rows_input = self._row(c1, 3, "Scans folder", self.v_input, "dir", "All sub-folders are searched.")
        self.rows_study_id = self._row(c1, 4, "New ID", self.v_study_id, None, "Applied to every file in the folder. Letters, digits, - _ . only.", width=24)
        self.rows_mapping = self._row(c1, 5, "ID spreadsheet", self.v_mapping, "file", "CSV or Excel with an old-ID column and a new-ID column. Column names are auto-detected; see Advanced if not.",
                                      [("Spreadsheets", "*.csv *.xlsx *.xlsm"), ("All files", "*")])
        adv1 = ttk.Frame(c1)
        adv1.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        adv1.columnconfigure(1, weight=1)
        self.adv_frames = [adv1]
        self.rows_remap = self._row(adv1, 0, "Drive path fix", self.v_remap, None, "Only when the patient list was written on another computer: OLD=NEW prefix, e.g. /Volumes/Drive=E:\\", width=30)
        self.rows_picks = self._row(adv1, 1, "Already-analysed series", self.v_series_pick, "file", "CSV (study_id, series_uid, series_description) naming the series that must be kept for a patient.", csv_t)
        cols = ttk.Frame(adv1)
        cols.grid(row=2, column=1, columnspan=3, sticky="w", pady=6)
        lbl = ttk.Label(adv1, text="Spreadsheet columns")
        lbl.grid(row=2, column=0, sticky="w", padx=(0, 12), pady=6)
        self.rows_cols = [lbl, cols]
        for i, (text, var, width) in enumerate((("old ID", self.v_current_col, 12), ("new ID", self.v_new_col, 12), ("sheet", self.v_sheet, 8))):
            ttk.Label(cols, text=text).grid(row=0, column=2 * i, sticky="w", padx=(0 if i == 0 else 12, 4))
            ttk.Entry(cols, textvariable=var, width=width).grid(row=0, column=2 * i + 1, sticky="w")
        ttk.Label(cols, text="old ID is the").grid(row=0, column=6, sticky="w", padx=(12, 4))
        ttk.Combobox(cols, textvariable=self.v_match_on_label, values=tuple(MATCH_ON_LABELS.values()), state="readonly", width=19).grid(row=0, column=7, sticky="w")

        # ---- 2 output
        c2 = self._card(t, 1, "2   Output")
        self._row(c2, 0, "Folder", self.v_output, "dir", "Where the anonymised copies go: one folder per patient plus _logs and _review. Use a different drive from the scans, or at least a folder outside them.")
        ttk.Label(c2, textvariable=self.v_out_status, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w")

        # ---- 3 options
        c3 = self._card(t, 2, "3   Options")
        ttk.Label(c3, text="Profile").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=6)
        prow = ttk.Frame(c3)
        prow.grid(row=0, column=1, columnspan=3, sticky="w", pady=6)
        self.cb_profile = ttk.Combobox(prow, textvariable=self.v_profile_label, state="readonly", width=40)
        self.cb_profile.pack(side="left")
        self.cb_profile.bind("<<ComboboxSelected>>", lambda _e: self._profile_chosen())
        ttk.Button(prow, text="Edit...", command=self._edit_profiles).pack(side="left", padx=(8, 0))
        q = ttk.Label(prow, text="?", style="Muted.TLabel", cursor="question_arrow")
        q.pack(side="left", padx=(8, 0))
        Tooltip(q, "What to keep beyond the default. 'Blinded read' removes everything identifying. Other profiles may keep sex, a 5-year age bucket, shifted dates or the scanner model, within what DICOM PS3.15 allows.")
        sw = ttk.Frame(c3)
        sw.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.cb_ctca = ttk.Checkbutton(sw, text="Coronary series only", variable=self.v_ctca, style=switch)
        self.cb_ctca.pack(side="left", padx=(0, 24))
        self.cb_resume = ttk.Checkbutton(sw, text="Skip patients already done", variable=self.v_resume, style=switch)
        self.cb_resume.pack(side="left", padx=(0, 24))
        self.lbl_manifest_only = ttk.Label(sw, text="", style="Muted.TLabel")
        self.lbl_manifest_only.pack(side="left")
        adv3 = ttk.Frame(c3)
        adv3.grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.adv_frames.append(adv3)
        ttk.Checkbutton(adv3, text="Check output when done", variable=self.v_verify_after, style=switch).pack(side="left", padx=(0, 24))
        ttk.Checkbutton(adv3, text="Keep scanner technical details", variable=self.v_keep_tech, style=switch).pack(side="left", padx=(0, 24))
        ttk.Checkbutton(adv3, text="No series sub-folders", variable=self.v_flat, style=switch).pack(side="left")
        q3 = ttk.Label(adv3, text="?", style="Muted.TLabel", cursor="question_arrow")
        q3.pack(side="left", padx=(8, 0))
        Tooltip(q3, "Check output when done: runs the verify step automatically. It reads headers only, so it is safe on any cohort size; allow a few minutes per 100 patients.\n"
                    "Keep scanner technical details: kernel and scan options. Off for a blinded read.\nNo series sub-folders: one flat folder per patient.")

        # ---- actions
        act = ttk.Frame(t)
        act.grid(row=3, column=0, sticky="ew", pady=(4, 6))
        self.b_dry = ttk.Button(act, text="Preview", width=14, command=lambda: self._start_run(dry_run=True))
        self.b_start = ttk.Button(act, text="Anonymise", width=16, style=accent, command=self._start_run)
        self.b_stop = ttk.Button(act, text="Stop", width=8, command=self._stop, state="disabled")
        self.b_dry.pack(side="left", ipady=4)
        self.b_start.pack(side="left", padx=(10, 0), ipady=4)
        self.b_stop.pack(side="left", padx=(10, 0), ipady=4)
        self.b_viewer = ttk.Button(act, text="Open viewer", command=lambda: self._viewer("source"))
        self.b_viewer.pack(side="left", padx=(28, 0), ipady=4)
        ttk.Checkbutton(act, text="Advanced", variable=self.v_advanced, command=self._apply_mode, style=toggle).pack(side="right")
        more = ttk.Menubutton(act, text="More")
        mm = tk.Menu(more, tearoff=False)
        mm.add_command(label="Show the command this will run", command=self._show_command)
        mm.add_command(label="Open output folder", command=lambda: self._open(self._out()))
        mm.add_command(label="Open log file", command=lambda: self._open(self.log_path))
        mm.add_command(label="Copy log", command=self._copy_log)
        mm.add_separator()
        mm.add_checkbutton(label="Show patients skipped as already done", variable=self.v_show_all)
        more["menu"] = mm
        more.pack(side="right", padx=(0, 10))
        self.v_ready = tk.StringVar(value="")
        self.lbl_ready = ttk.Label(t, textvariable=self.v_ready, wraplength=900, justify="left")
        self.lbl_ready.grid(row=4, column=0, sticky="w", pady=(0, 4))

        prog = ttk.Frame(t)
        prog.grid(row=5, column=0, sticky="ew", pady=(2, 4))
        prog.columnconfigure(0, weight=1)
        ttk.Progressbar(prog, variable=self.v_progress, maximum=100).grid(row=0, column=0, sticky="ew")
        ttk.Label(prog, textvariable=self.v_progress_text, style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))

        logf = ttk.Frame(t)
        logf.grid(row=6, column=0, sticky="nsew")
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log = tk.Text(logf, font=MONO, wrap="none", state="disabled", height=5, width=60, undo=False)
        ys = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ys.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")

    def _apply_mode(self) -> None:
        mode = self.v_mode.get()
        adv = self.v_advanced.get()
        groups = {
            "manifest": self.rows_manifest,
            "single": self.rows_input + self.rows_study_id,
            "mapping": self.rows_input + self.rows_mapping,
        }
        adv_groups = {
            "manifest": self.rows_remap + self.rows_picks,
            "single": [],
            "mapping": self.rows_cols,
        }
        every = set(sum(groups.values(), []) + sum(adv_groups.values(), []))
        show = set(groups.get(mode, [])) | (set(adv_groups.get(mode, [])) if adv else set())
        for w in every:
            (w.grid if w in show else w.grid_remove)()
        for f in self.adv_frames:
            (f.grid if adv else f.grid_remove)()
        manifest = mode == "manifest"
        for cb in (self.cb_ctca, self.cb_resume):
            cb.state(["!disabled"] if manifest else ["disabled"])
        self.lbl_manifest_only.configure(text="" if manifest else "(patient-list runs only)")
        self.lbl_mode.configure(text=model.MODE_HELP.get(mode, ""))

    # ------------------------------------------------------------------ Series tab
    def _tree(self, parent, columns: list[tuple[str, str, int]], height=12) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
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
        for tag, colour in COLOURS.items():
            if colour:
                tv.tag_configure(tag, foreground=colour)
        return tv

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
        self.tv_series = self._tree(t, [("study_id", "New ID", 100), ("series_number", "Series", 55), ("original_description", "Original series name", 220),
                                        ("images", "Images", 60), ("decision", "Decision", 70), ("reason", "Why", 300), ("run", "Run", 110)])
        self.lbl_series = ttk.Label(t, text="", style="Muted.TLabel")
        self.lbl_series.pack(anchor="w", pady=(6, 0))
        self.series_rows: list[dict] = []

    # ------------------------------------------------------------------ Verify tab
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
        self.txt_verify = tk.Text(t, font=MONO, wrap="none", height=14, state="disabled")
        self.txt_verify.pack(fill="both", expand=True)
        ttk.Label(t, text="Per-patient summary of the latest run").pack(anchor="w", pady=(10, 4))
        self.tv_summary = self._tree(t, [("study_id", "New ID", 100), ("status", "Status", 110), ("files", "Files", 55), ("series", "Series", 55),
                                         ("review_files", "To _review", 75), ("errors", "Errors", 55), ("source_folder", "Original folder (confidential)", 300)], height=8)

    # ------------------------------------------------------------------ Share tab
    def _build_share_tab(self) -> None:
        t = self.tab_share
        ttk.Label(t, text="Is the output folder safe to hand over?", style="H2.TLabel").pack(anchor="w")
        ttk.Label(t, text="The whole _logs folder is confidential. Move it out before the output leaves this computer.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 6))
        self.tv_checks = self._tree(t, [("mark", "", 28), ("title", "Check", 300), ("detail", "Detail", 500)], height=7)
        row = ttk.Frame(t)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="Refresh", command=self._refresh_share).pack(side="left")
        ttk.Button(row, text="Move confidential logs out...", command=self._move_logs).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Open _logs", command=lambda: self._open(self._logs())).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Review quarantined files...", command=lambda: self._viewer("output")).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Open _review folder", command=lambda: self._open(self._out() / "_review" if self._out() else None)).pack(side="left", padx=(8, 0))
        ttk.Label(t, text="Files in the logs folder (_logs)").pack(anchor="w", pady=(6, 4))
        self.tv_logs = self._tree(t, [("name", "File", 300), ("size", "Size", 70), ("modified", "Modified", 130), ("note", "", 320)], height=8)

    # ------------------------------------------------------------------ Help tab
    def _build_help_tab(self) -> None:
        t = self.tab_help
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
                       profile=self.v_profile.get())

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
        for b in (self.b_dry, self.b_start, self.b_verify):
            b.state(["disabled"] if running else ["!disabled"])
        self.b_stop.state(["!disabled"] if running else ["disabled"])
        if not running:
            self._live_validate()

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
        self.skipped_hidden = 0
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
        self._save_settings()
        self.nb.select(self.tab_run)

    def _start_run(self, dry_run: bool = False) -> None:
        spec = self._spec(dry_run)
        problems = spec.validate()
        if problems:
            messagebox.showerror("Cannot start", "\n".join(problems))
            return
        cmd = spec.command()
        if not dry_run:
            msg = (f"Output: {spec.output}\n\nOriginals are never modified. The engine will run as:\n\n{shown_command(cmd)}\n\nStart?")
            if not messagebox.askokcancel("Anonymise", msg):
                return
        self._start_job("dry" if dry_run else "run", ["run", *spec.run_args()], None if dry_run else "run", "Preview" if dry_run else "Anonymisation")

    def _start_verify(self) -> None:
        out = self._out()
        if not out or not out.is_dir():
            messagebox.showerror(APP_NAME, "Choose an existing output folder first.")
            return
        args = ["verify", *model.verify_args(str(out), model.split_needles(self.v_needles.get()), self.v_keep_tech.get(), self.v_profile.get())]
        self._start_job("verify", args, "verify", "Output check")

    def _start_thick(self, fix: bool) -> None:
        out = self._out()
        if not out or not out.is_dir():
            messagebox.showerror(APP_NAME, "Choose an existing output folder first.")
            return
        if fix and not messagebox.askyesno("Clear thick studies?",
                                           f"This deletes, from the OUTPUT folder only, every finished study whose kept series are thicker than "
                                           f"{model.MAX_SLICE_MM:g} mm, so the next run with Resume redoes them under the current rule.\n\n"
                                           f"Original scans are not touched.\n\nOutput: {out}\n\nContinue?", icon="warning", default="no"):
            return
        self._start_job("thick", ["thick", *model.thick_args(str(out), fix)], "thick", "Thickness audit")

    def _stop(self) -> None:
        if not (self.proc and self.proc.running):
            return
        if self.job == "run" and not messagebox.askyesno("Stop?", "The patient in progress is left unfinished and will be redone next time with 'Skip patients already done' ticked.\n\nStop now?"):
            return
        self.proc.stop()
        self._append_log("** stopped by user **", "warn")

    def _finish(self) -> None:
        assert self.proc is not None
        rc = self.proc.returncode
        secs = self.proc.elapsed()
        took = f"{secs / 60:.1f} min" if secs >= 90 else f"{secs:.0f} s"
        job, self.job = self.job, None
        titles = {"run": "Anonymisation", "dry": "Preview", "verify": "Output check", "thick": "Thickness audit"}
        title = titles.get(job or "", "Job")
        if rc == 0:
            self.v_progress_text.set(f"{title} finished in {took}")
            self.v_status.set(f"{title} finished")
            if job == "run":
                self.v_progress.set(100)
        elif rc in (-15, 143, 130, 1) and self.log.get("end-2l", "end").strip().endswith("stopped by user **"):
            self.v_progress_text.set(f"{title} stopped after {took}")
            self.v_status.set(f"{title} stopped")
        else:
            self.v_progress_text.set(f"{title} finished with problems (exit code {rc}) after {took}; read the log")
            self.v_status.set(f"{title}: problems, read the log")
            if job == "verify":
                messagebox.showwarning("Output check failed", "Identifiers were found in the output. Do not share it until the check passes. See the 'Verify output' tab.")
        self._set_running(False)
        self._refresh_all()
        if self.skipped_hidden and not self.v_show_all.get():
            self._append_log(f"({self.skipped_hidden} lines for studies skipped by Resume are hidden; tick 'Show lines...' to see them)", "plain")
        if job == "run" and rc == 0 and self.v_verify_after.get():
            self.after(400, self._start_verify)
        elif job == "verify":
            self.nb.select(self.tab_verify)

    def _poll(self) -> None:
        if self.proc is not None and self.job is not None:
            for line in self.proc.poll_lines():
                self._handle_line(line)
            if self.proc.done:
                for line in self.proc.poll_lines():
                    self._handle_line(line)
                self._finish()
        self.after(POLL_MS, self._poll)

    def _handle_line(self, line: str) -> None:
        if model.is_skipped_line(line) and not self.v_show_all.get():
            self.skipped_hidden += 1
            return
        prog = model.parse_progress(line)
        if prog:
            self.v_progress.set(prog.percent)
            self.v_progress_text.set(prog.text)
        self._append_log(line, model.classify_line(line))

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

    def _apply_theme_colours(self) -> None:
        pal = theme.palette()
        for w in (self.log, self.txt_verify, self.txt_help):
            theme.style_text(w)
        theme.tag_colours(self.log, ("error", "warn", "ok"))
        for tv in (self.tv_series, self.tv_summary, self.tv_checks, self.tv_logs):
            theme.tag_colours(tv, ("keep", "drop", "check", "block", "error", "ok", "warn"))
        txt = self.lbl_verify.cget("text")
        self.lbl_verify.configure(foreground=pal["ok"] if txt.startswith("PASS") else (pal["error"] if txt.startswith("FAIL") else pal["text"]))

    def _toggle_theme(self) -> None:
        self.theme_mode = theme.toggle(self)
        self.settings.set("theme", self.theme_mode)
        self.b_theme.configure(text="Dark" if self.theme_mode == "light" else "Light")
        self._apply_theme_colours()
        for w in self._viewers:
            try:
                w.apply_theme()
            except (tk.TclError, AttributeError):
                pass

    def _watch_form(self) -> None:
        self._validate_id: str | None = None
        for v in (self.v_mode, self.v_output, self.v_manifest, self.v_remap, self.v_input, self.v_study_id, self.v_mapping,
                  self.v_current_col, self.v_new_col, self.v_sheet, self.v_match_on, self.v_series_pick, self.v_profile):
            v.trace_add("write", lambda *_: self._schedule_validate())
        self._live_validate()

    def _schedule_validate(self) -> None:
        if self._validate_id:
            self.after_cancel(self._validate_id)
        self._validate_id = self.after(250, self._live_validate)

    def _live_validate(self) -> None:
        self._validate_id = None
        problems = self._spec().validate()
        running = bool(self.proc and self.proc.running)
        if problems:
            self.v_ready.set("Before you can start: " + "  ·  ".join(problems[:3]) + ("  ·  ..." if len(problems) > 3 else ""))
            self.lbl_ready.configure(style="Warn.TLabel")
        else:
            self.v_ready.set("Ready. Preview first, then Anonymise.")
            self.lbl_ready.configure(style="Ok.TLabel")
        if not running:
            for b in (self.b_dry, self.b_start):
                b.state(["!disabled"] if not problems else ["disabled"])

    def _refresh_profile_label(self) -> None:
        self._profile_entries = model.list_profiles()
        self.cb_profile["values"] = [e.label for e in self._profile_entries]
        cur = self.v_profile.get().strip()
        match = next((e for e in self._profile_entries if (str(e.path) if e.path else "") == cur), None)
        if match is None:                       # a profile file that no longer exists: fall back to the default
            match = self._profile_entries[0] if self._profile_entries else None
            self.v_profile.set("")
        if match:
            self.v_profile_label.set(match.label)
            self.v_profile_desc.set(match.profile.describe() + "  Written into every file as: " + match.profile.method_string(model.ENGINE_VERSION))
            if hasattr(self, "v_status"):
                self.v_status.set(self.v_profile_desc.get())

    def _profile_chosen(self) -> None:
        label = self.v_profile_label.get()
        e = next((e for e in self._profile_entries if e.label == label), None)
        if e:
            self.v_profile.set(str(e.path) if e.path else "")
            self._refresh_profile_label()

    def _edit_profiles(self) -> None:
        ProfileEditor(self, self.v_profile.get())

    def _viewer(self, mode: str, study_id: str | None = None) -> None:
        w = open_viewer(self, mode, study_id)
        if w is not None:
            self._viewers.append(w)

    def _viewer_selected_series(self) -> None:
        sel = self.tv_series.selection()
        sid = self.tv_series.set(sel[0], "study_id") if sel else None
        self._viewer("source", sid)

    def _show_command(self) -> None:
        spec = self._spec()
        problems = spec.validate()
        text = shown_command(spec.command())
        if problems:
            text += "\n\nNot runnable yet:\n- " + "\n- ".join(problems)
        messagebox.showinfo("Command", text)

    # ================================================================== refresh
    def _refresh_all(self) -> None:
        self._refresh_output_status()
        self._refresh_series()
        self._refresh_verify()
        self._refresh_share()

    def _schedule_output_refresh(self) -> None:
        if self._out_refresh_id:
            self.after_cancel(self._out_refresh_id)
        self._out_refresh_id = self.after(500, self._refresh_all)

    def _refresh_output_status(self) -> None:
        out = self._out()
        if not out:
            self.v_out_status.set("Choose an output folder. It will hold one folder per patient (named by new ID), plus _logs and _review.")
            return
        if not out.is_dir():
            self.v_out_status.set("Folder does not exist yet; it will be created on the first run.")
            return
        done, partial = model.study_state(out)
        n = model.manifest_count(self.v_manifest.get()) if self.v_mode.get() == "manifest" else None
        s = f"Patients done: {len(done)}   ·   half-finished (will be redone): {len(partial)}"
        if n is not None:
            s += f"   ·   patients in the list: {n}"
        self.v_out_status.set(s)

    def _refresh_series(self) -> None:
        logs = self._logs()
        self.series_rows = model.load_series_rows(logs) if logs and logs.is_dir() else []
        rows = model.filter_series(self.series_rows, self.v_series_filter.get(), self.v_series_query.get())
        tv = self.tv_series
        tv.delete(*tv.get_children(""))
        for r in rows:
            tag = "check" if model.needs_check(r) else ("keep" if r.get("decision") == "keep" else "drop")
            tv.insert("", "end", values=[r.get(c, "") for c in tv["columns"]], tags=(tag,))
        c = model.series_counts(self.series_rows)
        if self.series_rows:
            self.lbl_series.configure(text=f"{len(rows)} shown of {len(self.series_rows)} series across {c['studies']} studies: "
                                           f"{c['kept']} kept, {c['dropped']} dropped, {c['check']} need a look")
        else:
            self.lbl_series.configure(text="Nothing yet. Anonymise a patient list with 'Coronary series only' on.")

    def _refresh_verify(self) -> None:
        logs = self._logs()
        status, rep, txt = model.verify_status(logs) if logs and logs.is_dir() else (None, None, "")
        if status == "PASS":
            self.lbl_verify.configure(text=f"PASS  ({rep.name})", foreground=theme.palette()["ok"])
        elif status == "FAIL":
            self.lbl_verify.configure(text=f"FAIL: residual identifiers found  ({rep.name})", foreground=theme.palette()["error"])
        else:
            self.lbl_verify.configure(text="Not checked yet.", foreground=theme.palette()["text"])
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

    def _refresh_share(self) -> None:
        out, logs = self._out(), self._logs()
        tv = self.tv_checks
        tv.delete(*tv.get_children(""))
        checks = model.share_readiness(out) if out else [model.Check("block", "Choose an output folder")]
        for c in checks:
            tv.insert("", "end", values=[LEVEL_MARK.get(c.level, ""), c.title, c.detail], tags=(c.level,))
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
        dest = filedialog.askdirectory(title="Choose a folder OUTSIDE the output tree for the confidential logs", mustexist=True)
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

    # ================================================================== lifecycle
    def _on_close(self) -> None:
        if self.proc and self.proc.running:
            if not messagebox.askyesno("Quit?", "A job is running. Stop it and quit?"):
                return
            self.proc.stop()
        self._save_settings()
        self.destroy()

    def _selftest_walk(self) -> None:
        for tab in (self.tab_run, self.tab_series, self.tab_verify, self.tab_share, self.tab_help):
            self.nb.select(tab)
            self.update()
        for mode in model.MODES:
            self.v_mode.set(mode)
            self._apply_mode()
            self.update()
        self.v_mode.set("manifest")
        self._apply_mode()
        self.nb.select(self.tab_run)
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
        print("selftest: window built, all tabs drawn, viewer opened, profile editor opened", flush=True)
        self._selftest_viewer_on_synthetic_patients()
        self.after(200, self.destroy)

    def _selftest_viewer_on_synthetic_patients(self) -> None:
        """Build the synthetic patients, load one into the viewer, and make sure series, pixels and the header
        diff all come through. Runs inside the frozen app at build time, so a broken bundle cannot ship."""
        import shutil
        import tempfile
        import time
        try:
            from scrubdicom import fixtures
        except ImportError as e:
            print(f"selftest: fixtures unavailable ({e}); viewer data check skipped", flush=True)
            return
        tmp = Path(tempfile.mkdtemp(prefix="scrubdicom_selftest_"))
        try:
            import contextlib
            import io
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
