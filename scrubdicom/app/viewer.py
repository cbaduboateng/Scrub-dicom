"""The in-app DICOM viewer window.

Two modes, one window:
  Original scans      preview of what Anonymise will do: series list with keep/drop decisions, the images, and
                      the header before/after with every change highlighted. Computed in memory; nothing is
                      written. "Keep this series instead" records an override the engine reads (--series-pick).
  Anonymised output   the written copies plus the quarantined files in _review, with a redaction tool: draw
                      boxes over burned-in text, release the file into the study folder, then run the check again.

Viewing: bilinear rendering, zoom and pan, window/level presets and mouse drag, Hounsfield readout under the
cursor, cine playback, corner annotations, series thumbnails, and axial / coronal / sagittal reformats cut from
the loaded volume. Drawing uses tkinter.PhotoImage fed PGM bytes, so the viewer needs numpy (and GDCM for
compressed pixel data) but no Pillow. Everything that is not a widget lives in preview.py.
"""
from __future__ import annotations

import math
import queue
import sys
import threading
import tkinter as tk
from collections import OrderedDict
from pathlib import Path
from tkinter import messagebox, ttk

from . import model
from . import preview as pv
from . import theme

MONO = ("Menlo", 11) if sys.platform == "darwin" else ("Consolas", 10)
SMALL = ("Menlo", 10) if sys.platform == "darwin" else ("Consolas", 9)
COLOURS = {"keep": "#1a7f37", "maybe": "#9a6700", "drop": "#6e7781", "review": "#b42318",
           "removed": "#b42318", "changed": "#9a6700", "added": "#1a7f37", "kept": None, "override": "#0b5fff"}
MODES = ("Original scans (preview what Anonymise will do)", "Anonymised output (check, redact, release)")
ANNOT = "#e8d44d"
ZOOMS = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0)


class ViewerWindow(tk.Toplevel):
    def __init__(self, app, mode: str = "source", study_id: str | None = None):
        super().__init__(app)
        self.app = app
        self.title("Scrub-DICOM viewer")
        self.minsize(1280, 740)
        self.geometry("1400x860")
        self.spec = app._spec()
        self.out: Path | None = app._out()
        self.mode = tk.StringVar(value=MODES[0] if mode == "source" else MODES[1])
        self.patients: list[tuple[str, Path]] = []
        self.series: list[pv.Series] = []
        self.thumbs: dict[str, tk.PhotoImage] = {}
        self.cur: pv.Series | None = None
        self.idx = 0
        self.slice: pv.Slice | None = None
        self.view: "pv.np.ndarray | None" = None     # the 2-D array on screen (axial slice or a reformat)
        self.no_pixels = ""
        self.aspect = 1.0
        self.vol: pv.Volume | None = None
        self.vol_for: str | None = None
        self.cache: OrderedDict[tuple[str, int], pv.Slice] = OrderedDict()
        self.photo = None
        self.zoom, self.pan = 1.0, [0.0, 0.0]
        self.scale, self.off = 1.0, (0.0, 0.0)
        self.boxes: list[pv.Box] = []
        self.drag = None
        self.wl: tuple[float, float] | None = None
        self.playing = False
        self._q: queue.Queue = queue.Queue()
        self._mapping: dict[str, str] | None = None
        self.v_patient = tk.StringVar()
        self.v_orient = tk.StringVar(value="Axial")
        self.v_preset = tk.StringVar(value="From header")
        self.v_wc, self.v_ww = tk.StringVar(), tk.StringVar()
        self.v_slice = tk.IntVar(value=0)
        self.v_invert = tk.BooleanVar(value=False)
        self.v_annot = tk.BooleanVar(value=True)
        self.v_fps = tk.IntVar(value=12)
        self.v_changed_only = tk.BooleanVar(value=True)
        self.v_filter = tk.StringVar()
        self.v_redact = tk.BooleanVar(value=False)
        self.v_status = tk.StringVar(value="")
        self.v_summary = tk.StringVar(value="")
        self.v_readout = tk.StringVar(value="")
        self._build()
        self._load_patients(study_id)
        self.after(150, self._poll)
        self.after(60, self._init_sashes)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _init_sashes(self) -> None:
        """Series list ~300 px, header ~400 px, the image gets the rest; the user can still drag the sashes."""
        try:
            self.update_idletasks()
            total = self.pane.winfo_width()
            if total > 900:
                self.pane.sashpos(0, 360)
                self.pane.sashpos(1, total - 380)
        except tk.TclError:
            pass

    # ================================================================== layout
    def _build(self) -> None:
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Show").pack(side="left")
        for m in MODES:
            ttk.Radiobutton(top, text=m, value=m, variable=self.mode, command=lambda: self._load_patients(None)).pack(side="left", padx=(10, 0))
        ttk.Label(top, text="Patient").pack(side="left", padx=(24, 6))
        self.cb_patient = ttk.Combobox(top, textvariable=self.v_patient, state="readonly", width=20)
        self.cb_patient.pack(side="left")
        self.cb_patient.bind("<<ComboboxSelected>>", lambda _e: self._load_series())
        self.mb_ext = ttk.Menubutton(top, text="Open in viewer app")
        em = tk.Menu(self.mb_ext, tearoff=False)
        em.add_command(label="This file", command=lambda: self._open_external("file"))
        em.add_command(label="This series' folder", command=lambda: self._open_external("folder"))
        em.add_separator()
        em.add_command(label="Choose viewer application...", command=self._choose_viewer_app)
        em.add_command(label="Use the system default", command=lambda: self._set_viewer_app(""))
        self.mb_ext["menu"] = em
        self.mb_ext.pack(side="right")
        self._sync_viewer_app_label()

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=10)
        self.pane = pane

        # ---- left: series
        left = ttk.Frame(pane, padding=(0, 0, 6, 0))
        pane.add(left, weight=1)
        ttk.Label(left, text="Series", style="H2.TLabel").pack(anchor="w")
        # buttons and details are packed first, anchored to the bottom, so the list can never push them off screen
        lb = ttk.Frame(left)
        lb.pack(side="bottom", fill="x", pady=(6, 0))
        self.b_use_ticked = ttk.Button(lb, text="Anonymise only the ticked series", style=theme.style_or("Accent.TButton"), command=self._use_ticked)
        self.b_use_ticked.pack(side="left", ipady=4)
        self.b_clear_ticks = ttk.Button(lb, text="Let the rule decide", command=self._clear_ticks)
        self.b_clear_ticks.pack(side="left", padx=(6, 0), ipady=4)
        self.lbl_ticks = ttk.Label(left, text="", style="Muted.TLabel", wraplength=340, justify="left")
        self.lbl_ticks.pack(side="bottom", anchor="w", pady=(4, 0))
        self.lbl_detail = ttk.Label(left, text="", wraplength=340, justify="left", style="Muted.TLabel")
        self.lbl_detail.pack(side="bottom", anchor="w", pady=(6, 0))
        self.ticked: set[str] = set()
        self.b_override = ttk.Button(lb, text="Keep this series instead", command=self._override)   # rule-fallback override; kept for manifests with analysed picks
        self.b_unoverride = ttk.Button(lb, text="Undo", command=self._unoverride)
        tvf = ttk.Frame(left)
        tvf.pack(fill="both", expand=True)
        self.tv = ttk.Treeview(tvf, columns=("tick", "no", "desc", "n", "dec"), show=("tree", "headings"), height=5, selectmode="browse")
        self.tv.column("#0", width=72, minwidth=72, stretch=False)
        self.tv.heading("#0", text="")
        self.tv.heading("tick", text="Use")
        self.tv.column("tick", width=40, minwidth=40, stretch=False, anchor="center")
        self.tv.bind("<Button-1>", self._tick_click, add="+")
        for k, h, w in (("no", "S#", 44), ("desc", "Description", 180), ("n", "Imgs", 52), ("dec", "Decision", 96)):
            self.tv.heading(k, text=h)
            self.tv.column(k, width=w, minwidth=36, stretch=(k == "desc"))
        ttk.Style(self).configure("Viewer.Treeview", rowheight=68)
        self.tv.configure(style="Viewer.Treeview")
        ys = ttk.Scrollbar(tvf, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=ys.set)
        self.tv.pack(side="left", fill="both", expand=True)
        ys.pack(side="left", fill="y")
        self.tv.bind("<<TreeviewSelect>>", lambda _e: self._select_series())

        # ---- middle: image
        mid = ttk.Frame(pane)
        pane.add(mid, weight=4)
        toggle = theme.style_or("Toggle.TButton")
        tb = ttk.Frame(mid)
        tb.pack(fill="x", pady=(0, 2))
        tb2 = ttk.Frame(mid)
        tb2.pack(fill="x", pady=(0, 2))
        tb3 = ttk.Frame(mid)
        tb3.pack(fill="x", pady=(0, 4))
        # row 1: orientation and zoom buttons
        for o in pv.ORIENTATIONS:
            ttk.Radiobutton(tb, text=o, value=o, variable=self.v_orient, command=self._orient, style=toggle).pack(side="left", padx=(0, 4))
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(tb, text="Fit", width=4, command=lambda: self._set_zoom(1.0, reset_pan=True)).pack(side="left")
        ttk.Button(tb, text="1:1", width=4, command=self._one_to_one).pack(side="left", padx=(4, 0))
        ttk.Button(tb, text="+", width=3, command=lambda: self._zoom_step(1)).pack(side="left", padx=(4, 0))
        ttk.Button(tb, text="-", width=3, command=lambda: self._zoom_step(-1)).pack(side="left", padx=(4, 0))
        # row 2: what the mouse does, and the zoom slider
        ttk.Label(tb2, text="Mouse drag").pack(side="left")
        self.v_tool = tk.StringVar(value="Window/Level")
        for tool, text in (("Window/Level", "W/L"), ("Zoom", "Zoom"), ("Pan", "Pan")):
            ttk.Radiobutton(tb2, text=text, value=tool, variable=self.v_tool, command=self._redact_mode, style=toggle).pack(side="left", padx=(6, 0))
        ttk.Separator(tb2, orient="vertical").pack(side="left", fill="y", padx=8)
        self.v_zoom_pct = tk.DoubleVar(value=100.0)
        self.zoom_scale = ttk.Scale(tb2, from_=25, to=800, orient="horizontal", length=120, variable=self.v_zoom_pct, command=lambda _v: self._zoom_from_slider())
        self.zoom_scale.pack(side="left")
        self.lbl_zoom = ttk.Label(tb2, text="100%", width=5)
        self.lbl_zoom.pack(side="left")
        # row 3: cine and display toggles
        self.b_play = ttk.Button(tb3, text="Play", width=5, command=self._toggle_play)
        self.b_play.pack(side="left")
        ttk.Spinbox(tb3, from_=2, to=40, textvariable=self.v_fps, width=3).pack(side="left", padx=(4, 0))
        ttk.Label(tb3, text="fps").pack(side="left", padx=(2, 6))
        ttk.Checkbutton(tb3, text="Invert", variable=self.v_invert, command=self._render, style=theme.style_or("Switch.TCheckbutton")).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(tb3, text="Annotations", variable=self.v_annot, command=self._render, style=theme.style_or("Switch.TCheckbutton")).pack(side="left", padx=(8, 0))

        self.canvas = tk.Canvas(mid, bg="black", highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._render())
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda e: self._step(-1))
        self.canvas.bind("<Button-5>", lambda e: self._step(1))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._mouse_release)
        self.canvas.bind("<Shift-ButtonPress-1>", self._pan_press)
        self.canvas.bind("<Shift-B1-Motion>", self._pan_motion)
        self.canvas.bind("<ButtonPress-2>", self._pan_press)
        self.canvas.bind("<B2-Motion>", self._pan_motion)
        self.canvas.bind("<ButtonPress-3>", self._pan_press)
        self.canvas.bind("<B3-Motion>", self._pan_motion)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Double-Button-1>", lambda e: self._set_zoom(1.0, reset_pan=True))
        for key, fn in (("<Up>", lambda e: self._step(-1)), ("<Down>", lambda e: self._step(1)), ("<Left>", lambda e: self._step(-1)),
                        ("<Right>", lambda e: self._step(1)), ("<plus>", lambda e: self._zoom_step(1)), ("<equal>", lambda e: self._zoom_step(1)),
                        ("<minus>", lambda e: self._zoom_step(-1)), ("<space>", lambda e: self._toggle_play()), ("<Home>", lambda e: self._goto(0)),
                        ("<End>", lambda e: self._goto(10 ** 9)), ("<Prior>", lambda e: self._step(-10)), ("<Next>", lambda e: self._step(10))):
            self.bind(key, fn)

        ctl = ttk.Frame(mid, padding=(0, 6))
        ctl.pack(fill="x")
        self.slider = ttk.Scale(ctl, from_=0, to=0, orient="horizontal", variable=self.v_slice, command=lambda _v: self._slider())
        self.slider.pack(fill="x")
        row = ttk.Frame(ctl)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Window").pack(side="left")
        cb = ttk.Combobox(row, textvariable=self.v_preset, values=list(pv.WINDOW_PRESETS), state="readonly", width=22)
        cb.pack(side="left", padx=(6, 12))
        cb.bind("<<ComboboxSelected>>", lambda _e: self._preset())
        ttk.Label(row, text="centre").pack(side="left")
        e1 = ttk.Entry(row, textvariable=self.v_wc, width=7)
        e1.pack(side="left", padx=(4, 8))
        ttk.Label(row, text="width").pack(side="left")
        e2 = ttk.Entry(row, textvariable=self.v_ww, width=7)
        e2.pack(side="left", padx=(4, 12))
        for e in (e1, e2):
            e.bind("<Return>", lambda _e: self._manual_wl())
        ttk.Label(row, textvariable=self.v_readout, font=SMALL).pack(side="left", padx=(6, 0))
        self.lbl_slice = ttk.Label(row, text="", style="Muted.TLabel")
        self.lbl_slice.pack(side="right")
        red = ttk.Frame(ctl)
        red.pack(fill="x", pady=(6, 0))
        self.cb_redact = ttk.Checkbutton(red, text="Redact: drag boxes over burned-in text", variable=self.v_redact, command=self._redact_mode)
        self.cb_redact.pack(side="left")
        self.b_clear = ttk.Button(red, text="Clear boxes", command=self._clear_boxes)
        self.b_clear.pack(side="left", padx=(8, 0))
        self.b_release = ttk.Button(red, text="Apply and release this file from quarantine", command=lambda: self._do_release(False))
        self.b_release.pack(side="left", padx=(8, 0))
        self.b_release_all = ttk.Button(red, text="Apply to every file in this series", command=lambda: self._do_release(True))
        self.b_release_all.pack(side="left", padx=(8, 0))

        # ---- right: header
        right = ttk.Frame(pane, padding=(6, 0, 0, 0))
        pane.add(right, weight=2)
        hdr = ttk.Frame(right)
        hdr.pack(fill="x")
        self.lbl_header = ttk.Label(hdr, text="Header: before and after", style="H2.TLabel")
        self.lbl_header.pack(side="left")
        ttk.Checkbutton(hdr, text="changed only", variable=self.v_changed_only, command=self._fill_header).pack(side="right")
        f = ttk.Entry(hdr, textvariable=self.v_filter, width=14)
        f.pack(side="right", padx=(0, 8))
        f.bind("<KeyRelease>", lambda _e: self._fill_header())
        ttk.Label(hdr, text="find").pack(side="right", padx=(0, 4))
        thf = ttk.Frame(right)
        thf.pack(fill="both", expand=True)
        self.th = ttk.Treeview(thf, columns=("tag", "name", "before", "after", "act"), show="headings", selectmode="browse")
        for k, h, w in (("tag", "Tag", 105), ("name", "Name", 140), ("before", "Before", 160), ("after", "After", 160), ("act", "Action", 62)):
            self.th.heading(k, text=h)
            self.th.column(k, width=w, minwidth=40, stretch=k in ("before", "after"))
        hys = ttk.Scrollbar(thf, orient="vertical", command=self.th.yview)
        self.th.configure(yscrollcommand=hys.set)
        self.th.pack(side="left", fill="both", expand=True)
        hys.pack(side="left", fill="y")
        ttk.Label(right, textvariable=self.v_summary, style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        ttk.Label(self, textvariable=self.v_status, padding=(10, 4)).pack(fill="x")
        self.apply_theme()
        self._redact_mode()

    def apply_theme(self) -> None:
        theme.tag_colours(self.tv, ("keep", "maybe", "drop", "review", "override"))
        theme.tag_colours(self.th, ("removed", "changed", "added"))

    # ================================================================== patients and series
    @property
    def source_mode(self) -> bool:
        return self.mode.get() == MODES[0]

    def _load_patients(self, preselect: str | None) -> None:
        self.patients, self._mapping = [], None
        spec = self.spec
        if self.source_mode:
            if spec.mode == "manifest" and spec.manifest.strip():
                self.patients = pv.manifest_patients(spec.manifest, spec.remap)
            elif spec.mode == "single" and spec.input.strip():
                self.patients = [(spec.study_id.strip() or "NEW-ID", Path(spec.input))]
            elif spec.mode == "mapping" and spec.input.strip() and Path(spec.input).is_dir():
                try:
                    from scrubdicom.core import load_mapping
                    self._mapping = load_mapping(spec.mapping, spec.current_col or None, spec.new_col or None, spec.sheet or None) if spec.mapping.strip() else {}
                except (SystemExit, OSError):
                    self._mapping = {}
                self.patients = [(f"? {p.name}", p) for p in sorted(Path(spec.input).iterdir()) if p.is_dir() and not p.name.startswith((".", "_"))]
        elif self.out and self.out.is_dir():
            done, partial = model.study_state(self.out)
            self.patients = [(s, self.out / s) for s in done + partial]
        names = [sid for sid, _ in self.patients]
        self.cb_patient["values"] = names
        pick = preselect if preselect in names else (names[0] if names else "")
        self.v_patient.set(pick)
        manifest = self.source_mode and spec.mode == "manifest"
        self.b_override.state(["!disabled"] if manifest else ["disabled"])
        self.b_unoverride.state(["!disabled"] if manifest else ["disabled"])
        for b in (self.b_use_ticked, self.b_clear_ticks):
            b.state(["!disabled"] if self.source_mode else ["disabled"])
        self.lbl_header.configure(text="Header: before and after" if self.source_mode else "Header of the anonymised file")
        self.v_changed_only.set(self.source_mode)
        if pick:
            self._load_series()
        else:
            self._clear_all("No patients to show. " + ("Fill in Step 1 on the Anonymise tab first." if self.source_mode else "Nothing has been anonymised into this output folder yet."))

    def _clear_all(self, status: str = "") -> None:
        self._stop_play()
        self.series, self.cur, self.slice, self.view, self.boxes, self.vol, self.vol_for = [], None, None, None, [], None, None
        self.no_pixels = ""
        self.tv.delete(*self.tv.get_children(""))
        self.th.delete(*self.th.get_children(""))
        self.canvas.delete("all")
        self.lbl_slice.configure(text="")
        self.v_readout.set("")
        self.v_status.set(status)

    def _study_id(self) -> str:
        sid = self.v_patient.get()
        if sid.startswith("? ") and self._mapping is not None:
            folder = sid[2:]
            key = folder if self.spec.match_on == "folder" else (str(self.series[0].header.get("PatientID", "")) if self.series else "")
            return self._mapping.get(key.strip().upper(), "UNMAPPED")
        return sid

    def _load_series(self) -> None:
        sid = self.v_patient.get()
        folder = next((p for s, p in self.patients if s == sid), None)
        if not folder:
            return
        self._clear_all(f"Reading headers under {folder} ...")
        source, out, mode = self.source_mode, self.out, self.mode.get()

        def work():
            try:
                series = pv.scan_patient(folder, progress=lambda n: self._q.put(("progress", n))) if source else pv.scan_output_patient(out, sid)
                self._q.put(("series", mode, sid, series))
                for s in series:                                   # thumbnails afterwards, one file per series
                    mid = s.files[len(s.files) // 2]
                    img = pv.thumbnail(mid, 64)
                    if img is not None:
                        self._q.put(("thumb", mode, sid, s.uid, pv.to_ppm(img)))
            except Exception as e:
                self._q.put(("error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                msg = self._q.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self.v_status.set(f"Reading headers: {msg[1]} files ...")
                elif kind == "error":
                    self.v_status.set("Error: " + msg[1])
                elif kind == "series" and msg[1] == self.mode.get() and msg[2] == self.v_patient.get():
                    self._show_series(msg[3])
                elif kind == "thumb" and msg[1] == self.mode.get() and msg[2] == self.v_patient.get():
                    self._set_thumb(msg[3], msg[4])
                elif kind == "volume" and self.cur and msg[1] == self.cur.uid:
                    self.vol, self.vol_for = msg[2], msg[1]
                    self.v_status.set("Reformat ready.")
                    self._orient()
                elif kind == "volprog":
                    self.v_status.set(f"Loading volume for reformats: {msg[1]} / {msg[2]} slices ...")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._poll)

    def _show_series(self, series: list[pv.Series]) -> None:
        self.series = series
        self.thumbs = {}
        self.tv.delete(*self.tv.get_children(""))
        override = pv.current_override(self._picks_path(create=False), self._study_id()) if self.source_mode else ""
        saved = pv.read_selection(self._selection_path()).get(self._study_id()) if self.source_mode else None
        self.ticked = set(saved) if saved else {s.uid for s in series if s.verdict == "keep"}
        self.ticks_saved = saved is not None
        for i, s in enumerate(series):
            tag = s.verdict
            dec = {"keep": "keep", "maybe": "needs a look", "drop": "drop", "review": "quarantined"}.get(s.verdict, s.verdict)
            if override and override in (s.uid, s.description):
                dec, tag = "KEEP (override)", "override"
            if saved is not None:
                dec, tag = ("ticked", "keep") if s.uid in saved else ("not ticked", "drop")
            mark = "" if not self.source_mode else ("\u2611" if s.uid in self.ticked else "\u2610")
            self.tv.insert("", "end", iid=str(i), text="", values=(mark, s.number, s.description, s.n_images, dec), tags=(tag,))
        self._ticks_label()
        n = sum(s.n_images for s in series)
        self.v_status.set(f"{len(series)} series, {n} images. Study ID: {self._study_id()}")
        first = next((str(i) for i, s in enumerate(series) if s.verdict in ("keep", "review")), "0" if series else None)
        if first is not None:
            self.tv.selection_set(first)
            self.tv.see(first)

    def _selection_path(self) -> Path:
        return pv.selection_path(self.spec.confidential, self.spec.manifest)

    def _ticks_label(self) -> None:
        if not self.source_mode:
            self.lbl_ticks.configure(text="")
            return
        n = len(self.ticked)
        self.lbl_ticks.configure(text=(f"{n} series ticked and saved for this patient: only these will be anonymised." if getattr(self, "ticks_saved", False)
                                       else f"{n} series ticked (the rule's choice). Change the ticks and press the button to anonymise only those."))

    def _tick_click(self, e) -> None:
        if not self.source_mode or self.tv.identify_column(e.x) != "#1":
            return
        row = self.tv.identify_row(e.y)
        if row:
            self._toggle_tick(row)

    def _toggle_tick(self, row: str) -> None:
        s = self.series[int(row)]
        if s.uid in self.ticked:
            self.ticked.discard(s.uid)
        else:
            self.ticked.add(s.uid)
        self.tv.set(row, "tick", "\u2611" if s.uid in self.ticked else "\u2610")
        self.ticks_saved = False
        self._ticks_label()

    def _use_ticked(self) -> None:
        if not self.series:
            return
        chosen = [s for s in self.series if s.uid in self.ticked]
        if not chosen:
            messagebox.showinfo("Nothing ticked", "Tick at least one series in the 'Use' column.", parent=self)
            return
        path = pv.write_selection(self._selection_path(), self._study_id(), chosen)
        self.app.v_series_select.set(str(path))
        self.ticks_saved = True
        sid = self._study_id()
        self._close()
        self.app.ticks_saved(len(chosen), sid)

    def _clear_ticks(self) -> None:
        path = self._selection_path()
        if path.exists():
            pv.write_selection(path, self._study_id(), [])
            if not pv.read_selection(path):
                try:
                    path.unlink()
                except OSError:
                    pass
                self.app.v_series_select.set("")
        self.ticks_saved = False
        self._show_series(self.series)
        self.v_status.set("Selection cleared for this patient; the rule decides again.")

    def _set_thumb(self, uid: str, ppm: bytes) -> None:
        for i, s in enumerate(self.series):
            if s.uid == uid and self.tv.exists(str(i)):
                img = tk.PhotoImage(data=ppm)
                self.thumbs[uid] = img
                self.tv.item(str(i), image=img)

    def _select_series(self) -> None:
        sel = self.tv.selection()
        if not sel or int(sel[0]) >= len(self.series):
            return
        if self.cur is self.series[int(sel[0])]:
            return                      # Tk can deliver the same selection twice; do not reload or stop cine
        self._stop_play()
        self.cur = self.series[int(sel[0])]
        s = self.cur
        facts = [f"{s.thickness:g} mm" if s.thickness else "", f"{s.kvp} kV" if s.kvp else "", s.kernel, f"FOV {s.fov:.0f}" if s.fov else "", s.modality]
        self.lbl_detail.configure(text=f"S{s.number}  {s.description}\n" + "  ·  ".join(x for x in facts if x) + f"\n{s.reason}")
        self.idx, self.boxes, self.wl, self.vol, self.vol_for = 0, [], None, None, None
        self.zoom, self.pan = 1.0, [0.0, 0.0]
        self.v_orient.set("Axial")
        n = self.cur.n_images
        self.slider.configure(to=max(0, n - 1))
        self.v_slice.set(0)
        self.idx = n // 2
        self.v_slice.set(self.idx)
        self._load_current()
        self._fill_header()
        self._redact_mode()

    # ================================================================== slices and reformats
    def _current_path(self) -> Path | None:
        if not self.cur:
            return None
        if len(self.cur.files) == 1:
            return self.cur.files[0]
        return self.cur.files[min(self.idx, len(self.cur.files) - 1)]

    def _n_slices(self) -> int:
        if self.v_orient.get() != "Axial" and self.vol is not None:
            return pv.mpr_slice(self.vol, self.v_orient.get(), 0)[2]
        return self.cur.n_images if self.cur else 0

    def _load_current(self) -> None:
        if not self.cur:
            return
        if self.v_orient.get() != "Axial":
            if self.vol is None:
                return
            arr, aspect, n = pv.mpr_slice(self.vol, self.v_orient.get(), self.idx)
            self.view, self.aspect = arr, aspect
            self.lbl_slice.configure(text=f"{self.v_orient.get()}   {self.idx + 1} / {n}   {arr.shape[1]}x{arr.shape[0]}")
            if self.wl is None:
                self._preset(apply=False)
            self._render()
            return
        path = self._current_path()
        frame = self.idx if len(self.cur.files) == 1 else 0
        key = (str(path), frame)
        sl = self.cache.get(key)
        if sl is None:
            try:
                sl = pv.load_slice(path, frame)
            except Exception as e:
                self.slice, self.view = None, None
                self.no_pixels = f"Cannot display pixels:\n{e}\n\n(Structured reports and PDFs have no image;\ncompressed pixel data needs the GDCM decoder.)"
                self.lbl_slice.configure(text=path.name)
                self._render()
                return
            self.cache[key] = sl
            if len(self.cache) > 80:
                self.cache.popitem(last=False)
        self.slice = sl
        self.view = sl.array
        ps = self.cur.header.get("PixelSpacing")
        try:
            self.aspect = float(ps[0]) / float(ps[1]) if ps is not None and len(ps) == 2 and float(ps[1]) > 0 else 1.0
        except (TypeError, ValueError):
            self.aspect = 1.0
        if self.wl is None:
            self._preset(apply=False)
        self.lbl_slice.configure(text=f"{path.name}   {self.idx + 1} / {self.cur.n_images}   {sl.cols}x{sl.rows}")
        self._render()

    def _orient(self) -> None:
        if not self.cur:
            return
        o = self.v_orient.get()
        if o == "Axial":
            self.idx = min(self.idx, self.cur.n_images - 1)
            self.slider.configure(to=max(0, self.cur.n_images - 1))
            self._load_current()
            return
        if self.vol is None or self.vol_for != self.cur.uid:
            if len(self.cur.files) < 2:
                self.v_status.set("Reformats need a series of single-frame slices.")
                self.v_orient.set("Axial")
                return
            series = self.cur
            self.v_status.set("Loading volume for reformats ...")

            def work():
                try:
                    vol = pv.load_volume(series, progress=lambda i, n: self._q.put(("volprog", i, n)))
                    self._q.put(("volume", series.uid, vol))
                except Exception as e:
                    self._q.put(("error", f"Reformat failed: {e}"))
            threading.Thread(target=work, daemon=True).start()
            return
        n = pv.mpr_slice(self.vol, o, 0)[2]
        self.idx = n // 2
        self.slider.configure(to=max(0, n - 1))
        self.v_slice.set(self.idx)
        self.zoom, self.pan = 1.0, [0.0, 0.0]
        self._load_current()

    # ================================================================== rendering
    def _render(self) -> None:
        self.canvas.delete("all")
        arr = self.view
        if arr is None:
            if self.no_pixels:
                self.canvas.create_text(20, 20, anchor="nw", fill="#ddd", font=MONO, text=self.no_pixels)
            return
        self.no_pixels = ""
        cw, ch = max(10, self.canvas.winfo_width()), max(10, self.canvas.winfo_height())
        wc, ww = self.wl or (self.slice.default_window if self.slice and self.slice.default_window else None) or (40.0, 400.0)
        img8 = pv.window(arr, wc, ww)
        if self.v_invert.get() and img8.ndim == 2:
            img8 = 255 - img8
        rows, cols = img8.shape[:2]
        fit = min(cw / cols, ch / (rows * self.aspect))
        scale = fit * self.zoom
        full_w, full_h = cols * scale, rows * scale * self.aspect
        ox = (cw - full_w) / 2 + self.pan[0]
        oy = (ch - full_h) / 2 + self.pan[1]
        # only resample the part of the image that is on screen
        sx0 = max(0, int(math.floor((0 - ox) / scale)))
        sy0 = max(0, int(math.floor((0 - oy) / (scale * self.aspect))))
        sx1 = min(cols, int(math.ceil((cw - ox) / scale)) + 1)
        sy1 = min(rows, int(math.ceil((ch - oy) / (scale * self.aspect))) + 1)
        if sx1 <= sx0 or sy1 <= sy0:
            return
        crop = img8[sy0:sy1, sx0:sx1]
        small = pv.resample_to(crop, (sx1 - sx0) * scale, (sy1 - sy0) * scale * self.aspect, smooth=True)
        self.photo = tk.PhotoImage(data=pv.to_ppm(small))
        self.scale, self.off = scale, (ox, oy)
        self.canvas.create_image(ox + sx0 * scale, oy + sy0 * scale * self.aspect, anchor="nw", image=self.photo)
        for b in self.boxes:
            self._draw_box(b, "#ff3b30")
        if self.v_annot.get():
            self._annotate(cw, ch, wc, ww)
        if self.v_redact.get():
            self.canvas.create_text(cw // 2, 10, anchor="n", fill="#ff3b30", font=MONO, text="REDACT: drag a box over each piece of burned-in text")

    def _annotate(self, cw: int, ch: int, wc: float, ww: float) -> None:
        s = self.cur
        if not s:
            return
        tl = f"{self._study_id()}\nS{s.number}  {s.description[:40]}\n{s.modality}  {s.n_images} images"
        tr = f"{self.v_orient.get()}  {self.idx + 1} / {self._n_slices()}\nzoom {self.zoom * 100:.0f}%"
        bl = f"WL {wc:.0f} / WW {ww:.0f}" + ("  inverted" if self.v_invert.get() else "")
        br = "  ".join(x for x in ((f"{s.kvp} kV" if s.kvp else ""), (f"{s.mas} mAs" if s.mas else ""), (f"CTDIvol {s.ctdi}" if s.ctdi else "")) if x)
        br += ("\n" if br else "") + (f"{s.thickness:g} mm" if s.thickness else "") + (f"  {s.kernel}" if s.kernel else "")
        if self.v_orient.get() == "Axial" and self.slice is not None:
            ipp = None
            try:
                ipp = pydicom_ipp(self._current_path())
            except Exception:
                ipp = None
            if ipp:
                br += f"\nz {ipp:.1f} mm"
        for x, y, anchor, text in ((8, 8, "nw", tl), (cw - 8, 8, "ne", tr), (8, ch - 8, "sw", bl), (cw - 8, ch - 8, "se", br)):
            self.canvas.create_text(x + 1, y + 1, anchor=anchor, fill="black", font=SMALL, text=text, justify="left" if "w" in anchor else "right")
            self.canvas.create_text(x, y, anchor=anchor, fill=ANNOT, font=SMALL, text=text, justify="left" if "w" in anchor else "right")

    def _draw_box(self, b: pv.Box, colour: str) -> None:
        x0, y0 = self.off[0] + b.x0 * self.scale, self.off[1] + b.y0 * self.scale * self.aspect
        x1, y1 = self.off[0] + b.x1 * self.scale, self.off[1] + b.y1 * self.scale * self.aspect
        self.canvas.create_rectangle(x0, y0, x1, y1, outline=colour, width=2)
        self.canvas.create_rectangle(x0, y0, x1, y1, fill=colour, stipple="gray25", outline="")

    def _to_image(self, x: float, y: float) -> tuple[int, int]:
        return int((x - self.off[0]) / self.scale), int((y - self.off[1]) / (self.scale * self.aspect))

    # ================================================================== navigation
    def _goto(self, i: int) -> None:
        if not self.cur:
            return
        self.idx = max(0, min(self._n_slices() - 1, i))
        self.v_slice.set(self.idx)
        self._load_current()
        if self.source_mode and self.v_orient.get() == "Axial":
            self._fill_header()

    def _step(self, d: int) -> None:
        self._goto(self.idx + d)

    def _wheel(self, e) -> None:
        if e.state & 0x4 or e.state & 0x8 or e.state & 0x10:       # ctrl / cmd / alt: zoom about the cursor
            self._zoom_step(1 if e.delta > 0 else -1, at=(e.x, e.y))
        else:
            self._step(-1 if e.delta > 0 else 1)

    def _slider(self) -> None:
        i = int(round(float(self.v_slice.get())))
        if self.cur and i != self.idx:
            self.idx = i
            self._load_current()

    def _set_zoom(self, z: float, reset_pan: bool = False, at: tuple[int, int] | None = None) -> None:
        z = max(0.25, min(16.0, z))
        if at and self.view is not None:
            # keep the image point under the cursor fixed
            ix, iy = (at[0] - self.off[0]) / self.scale, (at[1] - self.off[1]) / self.scale
            factor = z / self.zoom
            self.pan[0] -= ix * self.scale * (factor - 1)
            self.pan[1] -= iy * self.scale * (factor - 1)
        if reset_pan:
            self.pan = [0.0, 0.0]
        self.zoom = z
        self._sync_zoom_widgets()
        self._render()

    def _sync_zoom_widgets(self) -> None:
        self._zoom_syncing = True
        try:
            self.v_zoom_pct.set(self.zoom * 100)
            self.lbl_zoom.configure(text=f"{self.zoom * 100:.0f}%")
        finally:
            self._zoom_syncing = False

    def _zoom_from_slider(self) -> None:
        if getattr(self, "_zoom_syncing", False):
            return
        z = float(self.v_zoom_pct.get()) / 100.0
        if abs(z - self.zoom) > 1e-3:
            self.zoom = max(0.25, min(16.0, z))
            self.lbl_zoom.configure(text=f"{self.zoom * 100:.0f}%")
            self._render()

    def _zoom_step(self, d: int, at=None) -> None:
        cur = self.zoom
        if d > 0:
            nxt = next((z for z in ZOOMS if z > cur + 1e-6), cur * 1.25)
        else:
            nxt = next((z for z in reversed(ZOOMS) if z < cur - 1e-6), cur / 1.25)
        self._set_zoom(nxt, at=at)

    def _one_to_one(self) -> None:
        if self.view is None:
            return
        cw, ch = max(10, self.canvas.winfo_width()), max(10, self.canvas.winfo_height())
        rows, cols = self.view.shape[:2]
        fit = min(cw / cols, ch / (rows * self.aspect))
        self._set_zoom(1.0 / fit, reset_pan=True)

    def _preset(self, apply: bool = True) -> None:
        p = pv.WINDOW_PRESETS.get(self.v_preset.get())
        if p is None:
            d = (self.vol.default_window if self.vol is not None and self.v_orient.get() != "Axial" else None) or \
                (self.slice.default_window if self.slice else None) or (40.0, 400.0)
            self.wl = d
        else:
            self.wl = p
        self.v_wc.set(f"{self.wl[0]:g}")
        self.v_ww.set(f"{self.wl[1]:g}")
        if apply:
            self._render()

    def _manual_wl(self) -> None:
        try:
            self.wl = (float(self.v_wc.get()), float(self.v_ww.get()))
        except ValueError:
            return
        self._render()

    def _toggle_play(self) -> None:
        if self.playing:
            self._stop_play()
        elif self.cur and self._n_slices() > 1:
            self.playing = True
            self.b_play.configure(text="Stop")
            self._play_tick()

    def _stop_play(self) -> None:
        self.playing = False
        try:
            self.b_play.configure(text="Play")
        except tk.TclError:
            pass

    def _play_tick(self) -> None:
        if not self.playing or not self.winfo_exists():
            return
        n = self._n_slices()
        self._goto((self.idx + 1) % max(1, n))
        self.after(max(25, int(1000 / max(1, self.v_fps.get()))), self._play_tick)

    # ================================================================== mouse
    def _hover(self, e) -> None:
        if self.view is None:
            return
        x, y = self._to_image(e.x, e.y)
        if 0 <= y < self.view.shape[0] and 0 <= x < self.view.shape[1]:
            v = self.view[y, x]
            txt = f"x {x}  y {y}   " + (f"HU {float(v):.0f}" if self.view.ndim == 2 else f"RGB {tuple(int(c) for c in v)}")
            self.v_readout.set(txt)
        else:
            self.v_readout.set("")

    def _press(self, e) -> None:
        self.focus_set()
        self.drag = (e.x, e.y, self.wl, self.zoom, list(self.pan))
        if self.v_redact.get():
            self._rubber = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3b30", width=2)

    def _motion(self, e) -> None:
        if not self.drag or self.view is None:
            return
        x0, y0, wl0, z0, p0 = self.drag
        tool = self.v_tool.get()
        if self.v_redact.get():
            self.canvas.coords(self._rubber, x0, y0, e.x, e.y)
        elif tool == "Zoom":
            self.pan = list(p0)
            self.zoom = z0
            self._set_zoom(z0 * math.exp((y0 - e.y) / 120.0), at=(x0, y0))
        elif tool == "Pan":
            self.pan = [p0[0] + e.x - x0, p0[1] + e.y - y0]
            self._render()
        else:
            wc, ww = wl0 or (self.slice.default_window if self.slice and self.slice.default_window else (40.0, 400.0))
            self.wl = (wc + (y0 - e.y) * 2.0, max(1.0, ww + (e.x - x0) * 4.0))
            self.v_wc.set(f"{self.wl[0]:g}")
            self.v_ww.set(f"{self.wl[1]:g}")
            self._render()

    def _mouse_release(self, e) -> None:
        if not self.drag:
            return
        x0, y0 = self.drag[0], self.drag[1]
        self.drag = None
        if self.v_redact.get() and self.view is not None:
            ax, ay = self._to_image(x0, y0)
            bx, by = self._to_image(e.x, e.y)
            b = pv.Box(ax, ay, bx, by).norm(self.view.shape[0], self.view.shape[1])
            if b.x1 - b.x0 >= 2 and b.y1 - b.y0 >= 2:
                self.boxes.append(b)
            self._render()

    def _pan_press(self, e) -> None:
        self._pan0 = (e.x, e.y, list(self.pan))

    def _pan_motion(self, e) -> None:
        x0, y0, p0 = self._pan0
        self.pan = [p0[0] + e.x - x0, p0[1] + e.y - y0]
        self._render()

    # ================================================================== redaction
    def _redact_mode(self) -> None:
        quarantined = bool(self.cur and self.cur.verdict == "review" and not self.source_mode and self.v_orient.get() == "Axial")
        for w in (self.cb_redact, self.b_clear, self.b_release, self.b_release_all):
            w.state(["!disabled"] if quarantined else ["disabled"])
        if not quarantined:
            self.v_redact.set(False)
        self.canvas.configure(cursor="tcross" if self.v_redact.get() else {"Zoom": "sizing", "Pan": "hand2"}.get(self.v_tool.get(), "fleur"))
        self._render()

    def _clear_boxes(self) -> None:
        self.boxes = []
        self._render()

    def _do_release(self, all_files: bool) -> None:
        if not (self.cur and self.cur.verdict == "review" and self.out):
            return
        files = list(self.cur.files) if all_files else [self._current_path()]
        what = f"{len(files)} file(s) of series S{self.cur.number}" if all_files else self._current_path().name
        if self.boxes:
            msg = (f"Paint {len(self.boxes)} box(es) black in {what} and move it out of quarantine into the study folder?\n\n"
                   "The original scan is not touched. Run the output check again afterwards.")
            if not messagebox.askyesno("Release from quarantine", msg, parent=self, icon="warning", default="no"):
                return
        else:
            from .ui import confirm_typed
            if not confirm_typed(self, "Release without redaction", f"No boxes drawn. {what} will leave quarantine unchanged. Only do this if you have looked at it and it carries no burned-in text."):
                return
        sid, done, errors = self._study_id(), 0, []
        for f in files:
            try:
                pv.release_from_quarantine(self.out, sid, f, list(self.boxes))
                done += 1
            except Exception as e:
                errors.append(f"{f.name}: {e}")
        self.cache.clear()
        self.boxes = []
        self.v_status.set(f"Released {done} file(s). Run the output check again before sharing." + (f"  Errors: {'; '.join(errors)}" if errors else ""))
        self.app._refresh_all()
        self._load_series()

    # ================================================================== header
    def _fill_header(self) -> None:
        self.th.delete(*self.th.get_children(""))
        path = self._current_path()
        if not path:
            return
        try:
            if self.source_mode:
                salt = pv.PREVIEW_SALT
                if self.out and (self.out / "_logs" / "uid_salt.txt").exists():
                    salt = (self.out / "_logs" / "uid_salt.txt").read_text().strip() or salt
                rows = pv.header_diff(path, self._study_id(), salt, self.spec.keep_technical, model.profile_for_path(self.spec.profile))
            else:
                rows = pv.header_rows(path)
        except Exception as e:
            self.v_summary.set(f"Cannot read header: {e}")
            return
        q = self.v_filter.get().strip().lower()
        shown = 0
        for r in rows:
            if self.v_changed_only.get() and r.action == "kept":
                continue
            if q and q not in (r.keyword + r.path + r.before + r.after).lower():
                continue
            self.th.insert("", "end", values=(r.path, r.keyword, r.before, r.after, r.action), tags=(r.action,))
            shown += 1
        c = pv.diff_summary(rows)
        self.v_summary.set(f"{shown} shown of {len(rows)} tags: {c['removed']} removed, {c['changed']} changed, {c['added']} added, {c['kept']} kept"
                           if self.source_mode else f"{shown} shown of {len(rows)} tags")

    # ================================================================== overrides, external viewer, close
    def _picks_path(self, create: bool) -> Path | None:
        if self.spec.mode != "manifest" or not self.spec.manifest.strip():
            return None
        if self.spec.series_pick.strip():
            p = Path(self.spec.series_pick)
        elif self.spec.confidential.strip():
            p = Path(self.spec.confidential).expanduser() / "series_picks.csv"
        else:
            p = pv.default_picks_path(self.spec.manifest)
        if create and not self.spec.series_pick.strip():
            self.app.v_series_pick.set(str(p))
            self.spec.series_pick = str(p)
        return p

    def _override(self) -> None:
        if not self.cur:
            return
        p = self._picks_path(create=True)
        if not p:
            messagebox.showinfo("Not available", "Series overrides work with a patient list (manifest). The engine reads them through --series-pick.", parent=self)
            return
        try:
            path, warning = pv.set_series_override(p, self._study_id(), self.cur)
        except OSError as e:
            messagebox.showerror("Could not save", str(e), parent=self)
            return
        self._show_series(self.series)
        msg = f"Recorded in {path.name}: when this patient is anonymised, series S{self.cur.number} \"{self.cur.description}\" is the one kept."
        if warning:
            messagebox.showwarning("Recorded, with a warning", msg + "\n\n" + warning, parent=self)
        else:
            self.v_status.set(msg)

    def _unoverride(self) -> None:
        p = self._picks_path(create=False)
        if p and p.exists():
            pv.remove_series_override(p, self._study_id())
            self._show_series(self.series)
            self.v_status.set("Override removed; the rule decides again.")

    def _open_external(self, what: str = "file") -> None:
        path = self._current_path()
        if not path:
            messagebox.showinfo("Nothing selected", "Select a series first.", parent=self)
            return
        from .ui import open_with
        open_with(path.parent if what == "folder" else path, self.app.settings.get("external_viewer") or "")

    def _choose_viewer_app(self) -> None:
        from tkinter import filedialog
        if sys.platform == "darwin":
            AppPicker(self, on_choose=self._set_viewer_app)       # .app bundles are folders: the file dialog greys them out
            return
        p = filedialog.askopenfilename(parent=self, title="Choose a DICOM viewer application",
                                       filetypes=[("Programs", "*.exe"), ("All files", "*")])
        if p:
            self._set_viewer_app(p)

    def _set_viewer_app(self, path: str) -> None:
        self.app.settings.set("external_viewer", path)
        self.app.settings.save()
        self._sync_viewer_app_label()

    def _sync_viewer_app_label(self) -> None:
        name = model.viewer_app_name(self.app.settings.get("external_viewer") or "")
        self.mb_ext.configure(text=f"Open in {name}")

    def _close(self) -> None:
        self._stop_play()
        self.destroy()


class AppPicker(tk.Toplevel):
    """Pick an installed macOS application by name (Bee DICOM Viewer, Horos, OsiriX, Weasis...)."""

    def __init__(self, parent, on_choose):
        super().__init__(parent)
        self.title("Choose a viewer application")
        self.geometry("460x520")
        self.transient(parent)
        self.on_choose = on_choose
        self.apps = model.find_applications()
        self.v_filter = tk.StringVar()
        ttk.Label(self, text="Applications", style="H2.TLabel").pack(anchor="w", padx=12, pady=(12, 4))
        f = ttk.Entry(self, textvariable=self.v_filter)
        f.pack(fill="x", padx=12)
        f.insert(0, "")
        f.bind("<KeyRelease>", lambda _e: self._fill())
        lf = ttk.Frame(self)
        lf.pack(fill="both", expand=True, padx=12, pady=8)
        self.lb = tk.Listbox(lf, exportselection=False)
        theme.style_text(self.lb)
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.lb.bind("<Double-Button-1>", lambda _e: self._choose())
        ttk.Label(self, text="Not listed? It is not installed on this Mac, or use 'Other folder...' to point at it.", style="Muted.TLabel", wraplength=420).pack(anchor="w", padx=12)
        b = ttk.Frame(self, padding=(12, 0, 12, 12))
        b.pack(fill="x")
        ttk.Button(b, text="Choose", style=theme.style_or("Accent.TButton"), command=self._choose).pack(side="left")
        ttk.Button(b, text="Other folder...", command=self._other).pack(side="left", padx=(8, 0))
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        self._fill()
        f.focus_set()
        for i, (name, _) in enumerate(self.shown):
            if "dicom" in name.lower() or name.lower() in ("horos", "osirix", "weasis", "microdicom"):
                self.lb.selection_set(i)
                self.lb.see(i)
                break

    def _fill(self) -> None:
        q = self.v_filter.get().strip().lower()
        self.shown = [(n, p) for n, p in self.apps if q in n.lower()]
        self.lb.delete(0, "end")
        for n, _ in self.shown:
            self.lb.insert("end", n)

    def _choose(self) -> None:
        sel = self.lb.curselection()
        if sel:
            self.on_choose(str(self.shown[sel[0]][1]))
            self.destroy()

    def _other(self) -> None:
        from tkinter import filedialog
        p = filedialog.askdirectory(parent=self, title="Choose the .app bundle (select it and press Choose)", initialdir="/Applications")
        if p:
            self.on_choose(p)
            self.destroy()


def pydicom_ipp(path: Path) -> float | None:
    import pydicom
    ds = pydicom.dcmread(str(path), stop_before_pixels=True, specific_tags=["ImagePositionPatient"], force=True)
    ipp = ds.get("ImagePositionPatient")
    return float(ipp[2]) if ipp is not None and len(ipp) == 3 else None


def open_viewer(app, mode: str = "source", study_id: str | None = None):
    ok, why = pv.viewer_available()
    if not ok:
        messagebox.showerror("Viewer not available", why)
        return None
    return ViewerWindow(app, mode, study_id)
