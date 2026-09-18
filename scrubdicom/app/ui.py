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
        self.minsize(1000, 720)
        geo = self.settings.get("geometry")
        if geo:
            try:
                self.geometry(geo)
            except tk.TclError:
                pass
        if sys.platform.startswith("linux"):
            ttk.Style(self).theme_use("clam")
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
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
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
                  foreground="#6e7781").pack(side="right")

    # ------------------------------------------------------------------ Run tab
    def _path_row(self, parent, r, label, var, kind=None, hint=None, filetypes=None):
        """One labelled entry row with an optional Browse button. Returns the widgets so a mode can hide them."""
        widgets = [ttk.Label(parent, text=label)]
        widgets[0].grid(row=r, column=0, sticky="w", padx=(0, 8), pady=3)
        e = ttk.Entry(parent, textvariable=var)
        e.grid(row=r, column=1, sticky="ew", pady=3)
        widgets.append(e)
        if kind:
            b = ttk.Button(parent, text="Browse...", command=lambda: self._browse(var, kind, filetypes))
            b.grid(row=r, column=2, padx=(6, 0), pady=3)
            widgets.append(b)
        if hint:
            h = ttk.Label(parent, text=hint, foreground="#6e7781")
            h.grid(row=r, column=3, sticky="w", padx=(8, 0))
            widgets.append(h)
        return widgets

    def _build_run_tab(self) -> None:
        t = self.tab_run
        t.columnconfigure(0, weight=1)
        t.rowconfigure(5, weight=1)

        inp = ttk.LabelFrame(t, text="Step 1  Where the scans are, and what each patient will be called", padding=8)
        inp.grid(row=0, column=0, sticky="ew")
        inp.columnconfigure(1, weight=1)
        for i, mode in enumerate(model.MODES):
            ttk.Radiobutton(inp, text=model.MODE_LABELS[mode], value=mode, variable=self.v_mode, command=self._apply_mode)\
                .grid(row=0, column=i, sticky="w", padx=(0, 16))
        self.lbl_mode = ttk.Label(inp, text="", foreground="#6e7781", wraplength=880, justify="left")
        self.lbl_mode.grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 0))
        fields = ttk.Frame(inp)
        fields.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        fields.columnconfigure(1, weight=1)
        csv_t = [("CSV files", "*.csv"), ("All files", "*")]
        self.rows_manifest = self._path_row(fields, 0, "Patient list (CSV)", self.v_manifest, "file", "two columns: source_folder, study_id (new ID)", csv_t)
        self.rows_remap = self._path_row(fields, 1, "Drive path fix (optional)", self.v_remap, None, "list written on another computer? e.g. /Volumes/Drive=E:\\")
        self.rows_input = self._path_row(fields, 2, "Scans folder", self.v_input, "dir", "all sub-folders are searched")
        self.rows_study_id = self._path_row(fields, 3, "New ID for this patient", self.v_study_id, None, "letters, digits, - _ . only")
        self.rows_mapping = self._path_row(fields, 4, "ID spreadsheet", self.v_mapping, "file", "CSV or Excel: old ID -> new ID",
                                           [("Spreadsheets", "*.csv *.xlsx *.xlsm"), ("All files", "*")])
        cols = ttk.Frame(fields)
        cols.grid(row=5, column=1, columnspan=3, sticky="w", pady=3)
        lbl = ttk.Label(fields, text="Spreadsheet columns")
        lbl.grid(row=5, column=0, sticky="w", padx=(0, 8), pady=3)
        self.rows_cols = [lbl, cols]
        for i, (text, var, width) in enumerate((("old ID", self.v_current_col, 12), ("new ID", self.v_new_col, 12), ("sheet", self.v_sheet, 8))):
            ttk.Label(cols, text=text).grid(row=0, column=2 * i, sticky="w", padx=(0 if i == 0 else 12, 4))
            ttk.Entry(cols, textvariable=var, width=width).grid(row=0, column=2 * i + 1, sticky="w")
        ttk.Label(cols, text="old ID is the").grid(row=0, column=6, sticky="w", padx=(12, 4))
        ttk.Combobox(cols, textvariable=self.v_match_on_label, values=tuple(MATCH_ON_LABELS.values()), state="readonly", width=19).grid(row=0, column=7, sticky="w")
        self.rows_picks = self._path_row(fields, 6, "Already-analysed series (optional)", self.v_series_pick, "file", "CSV: study_id, series_uid, series_description", csv_t)

        outf = ttk.LabelFrame(t, text="Step 2  Where the anonymised copies go", padding=8)
        outf.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        outf.columnconfigure(1, weight=1)
        self._path_row(outf, 0, "Output folder", self.v_output, "dir", "a different drive, or at least outside the scans")
        ttk.Button(outf, text="Open", command=lambda: self._open(self._out())).grid(row=0, column=3, padx=(6, 0))
        ttk.Label(outf, textvariable=self.v_out_status, foreground="#6e7781").grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        opt = ttk.LabelFrame(t, text="Step 3  Options", padding=8)
        opt.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.cb_ctca = ttk.Checkbutton(opt, text="Keep only the coronary CT angiogram series", variable=self.v_ctca)
        self.cb_resume = ttk.Checkbutton(opt, text="Skip patients already done (resume)", variable=self.v_resume)
        self.cb_ctca.grid(row=0, column=0, sticky="w", padx=(0, 24))
        self.cb_resume.grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(opt, text="Keep scanner technical details (kernel, scan options)", variable=self.v_keep_tech).grid(row=1, column=0, sticky="w", padx=(0, 24))
        ttk.Checkbutton(opt, text="One folder per patient, no series sub-folders", variable=self.v_flat).grid(row=1, column=1, sticky="w")
        ttk.Checkbutton(opt, text="Check the output automatically when done", variable=self.v_verify_after).grid(row=2, column=0, sticky="w", padx=(0, 24))
        self.lbl_manifest_only = ttk.Label(opt, text="", foreground="#6e7781")
        self.lbl_manifest_only.grid(row=2, column=1, sticky="w")

        btns = ttk.Frame(t)
        btns.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.b_dry = ttk.Button(btns, text="Preview (writes nothing)", command=lambda: self._start_run(dry_run=True))
        self.b_start = ttk.Button(btns, text="Anonymise", command=self._start_run)
        self.b_stop = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.b_dry.pack(side="left")
        self.b_start.pack(side="left", padx=(8, 0))
        self.b_stop.pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Preview a patient in the viewer...", command=lambda: self._viewer("source")).pack(side="left", padx=(24, 0))
        ttk.Button(btns, text="Show the command this will run", command=self._show_command).pack(side="right")

        prog = ttk.Frame(t)
        prog.grid(row=4, column=0, sticky="ew", pady=(10, 4))
        prog.columnconfigure(0, weight=1)
        ttk.Progressbar(prog, variable=self.v_progress, maximum=100).grid(row=0, column=0, sticky="ew")
        ttk.Label(prog, textvariable=self.v_progress_text).grid(row=1, column=0, sticky="w", pady=(3, 0))

        logf = ttk.Frame(t)
        logf.grid(row=5, column=0, sticky="nsew")
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log = tk.Text(logf, font=MONO, wrap="none", state="disabled", height=5, width=60, undo=False)
        ys = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        xs = ttk.Scrollbar(logf, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        for tag, colour in COLOURS.items():
            if colour:
                self.log.tag_configure(tag, foreground=colour)
        under = ttk.Frame(logf)
        under.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Checkbutton(under, text="Also show patients that were skipped as already done", variable=self.v_show_all).pack(side="left")
        ttk.Button(under, text="Open log file", command=lambda: self._open(self.log_path)).pack(side="right")
        ttk.Button(under, text="Copy log", command=self._copy_log).pack(side="right", padx=(0, 8))

    def _apply_mode(self) -> None:
        mode = self.v_mode.get()
        groups = {
            "manifest": self.rows_manifest + self.rows_remap + self.rows_picks,
            "single": self.rows_input + self.rows_study_id,
            "mapping": self.rows_input + self.rows_mapping + self.rows_cols,
        }
        every = set(sum(groups.values(), []))
        show = set(groups.get(mode, []))
        for w in every:
            (w.grid if w in show else w.grid_remove)()
        manifest = mode == "manifest"
        for cb in (self.cb_ctca, self.cb_resume):
            cb.state(["!disabled"] if manifest else ["disabled"])
        self.lbl_manifest_only.configure(text="" if manifest else "'Coronary only' and 'Skip patients already done' work with a patient list only.")
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
        ttk.Button(top, text="Open patient in viewer", command=self._viewer_selected_series).pack(side="right", padx=(0, 8))
        self.tv_series = self._tree(t, [("study_id", "New ID", 100), ("series_number", "Series", 55), ("original_description", "Original series name", 220),
                                        ("images", "Images", 60), ("decision", "Decision", 70), ("reason", "Why", 300), ("run", "Run", 110)])
        self.lbl_series = ttk.Label(t, text="", foreground="#6e7781")
        self.lbl_series.pack(anchor="w", pady=(6, 0))
        self.series_rows: list[dict] = []

    # ------------------------------------------------------------------ Verify tab
    def _build_verify_tab(self) -> None:
        t = self.tab_verify
        top = ttk.Frame(t)
        top.pack(fill="x")
        ttk.Label(top, text="Words that must not appear anywhere in the output, e.g. consultant surnames, hospital numbers (comma-separated; forgotten when you close the app)", wraplength=900).pack(anchor="w")
        row = ttk.Frame(top)
        row.pack(fill="x", pady=(4, 8))
        ttk.Entry(row, textvariable=self.v_needles).pack(side="left", fill="x", expand=True)
        self.b_verify = ttk.Button(row, text="Check the output now", command=self._start_verify)
        self.b_verify.pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Open report", command=lambda: self._open(model.verify_status(self._logs())[1] if self._logs() else None)).pack(side="left", padx=(8, 0))
        self.lbl_verify = ttk.Label(t, text="Not checked yet.", font=("TkDefaultFont", 14, "bold"))
        self.lbl_verify.pack(anchor="w", pady=(0, 6))
        self.txt_verify = tk.Text(t, font=MONO, wrap="none", height=14, state="disabled")
        self.txt_verify.pack(fill="both", expand=True)
        ttk.Label(t, text="Per-patient summary of the latest run").pack(anchor="w", pady=(10, 4))
        self.tv_summary = self._tree(t, [("study_id", "New ID", 100), ("status", "Status", 110), ("files", "Files", 55), ("series", "Series", 55),
                                         ("review_files", "To _review", 75), ("errors", "Errors", 55), ("source_folder", "Original folder (confidential)", 300)], height=8)

    # ------------------------------------------------------------------ Share tab
    def _build_share_tab(self) -> None:
        t = self.tab_share
        ttk.Label(t, text="Is the output folder safe to hand over?", font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        ttk.Label(t, text="Everything in _logs is confidential, not only the LINKAGE file: the per-file and per-study CSVs and the run logs "
                          "record the original folder paths. Move them out before the output folder leaves this computer.",
                  wraplength=900, foreground="#6e7781").pack(anchor="w", pady=(2, 6))
        self.tv_checks = self._tree(t, [("mark", "", 28), ("title", "Check", 300), ("detail", "Detail", 500)], height=7)
        row = ttk.Frame(t)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="Refresh", command=self._refresh_share).pack(side="left")
        ttk.Button(row, text="Move the patient-linking logs out...", command=self._move_logs).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Open the logs folder", command=lambda: self._open(self._logs())).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Review and redact quarantined files in the viewer...", command=lambda: self._viewer("output")).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Open _review folder", command=lambda: self._open(self._out() / "_review" if self._out() else None)).pack(side="left", padx=(8, 0))
        ttk.Label(t, text="Files in the logs folder (_logs)").pack(anchor="w", pady=(6, 4))
        self.tv_logs = self._tree(t, [("name", "File", 300), ("size", "Size", 70), ("modified", "Modified", 130), ("note", "", 320)], height=8)

    # ------------------------------------------------------------------ Help tab
    def _build_help_tab(self) -> None:
        t = self.tab_help
        txt = tk.Text(t, font=MONO, wrap="word", state="normal")
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
                       resume=self.v_resume.get(), keep_technical=self.v_keep_tech.get(), flat=self.v_flat.get(), dry_run=dry_run)

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
        args = ["verify", *model.verify_args(str(out), model.split_needles(self.v_needles.get()), self.v_keep_tech.get())]
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
            self.lbl_series.configure(text="Nothing to show yet. Anonymise a patient list with 'Keep only the coronary CT angiogram series' ticked.")

    def _refresh_verify(self) -> None:
        logs = self._logs()
        status, rep, txt = model.verify_status(logs) if logs and logs.is_dir() else (None, None, "")
        if status == "PASS":
            self.lbl_verify.configure(text=f"PASS  ({rep.name})", foreground=COLOURS["ok"])
        elif status == "FAIL":
            self.lbl_verify.configure(text=f"FAIL: residual identifiers found  ({rep.name})", foreground=COLOURS["error"])
        else:
            self.lbl_verify.configure(text="Not checked yet.", foreground="")
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
        print("selftest: window built, all tabs drawn, viewer opened", flush=True)
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
