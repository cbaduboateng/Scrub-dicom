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
        self.minsize(1180, 740)
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
        self.protocol("WM_DELETE_WINDOW", self._close)

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
        ttk.Button(top, text="Open this file in your DICOM viewer", command=self._open_external).pack(side="right")

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=10)

        # ---- left: series
        left = ttk.Frame(pane, padding=(0, 0, 6, 0))
        pane.add(left, weight=1)
        ttk.Label(left, text="Series", font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        tvf = ttk.Frame(left)
        tvf.pack(fill="both", expand=True)
        self.tv = ttk.Treeview(tvf, columns=("no", "desc", "n", "mm", "dec", "why"), show=("tree", "headings"), height=12, selectmode="browse")
        self.tv.column("#0", width=72, minwidth=72, stretch=False)
        self.tv.heading("#0", text="")
        for k, h, w in (("no", "S#", 40), ("desc", "Description", 170), ("n", "Imgs", 45), ("mm", "mm", 42), ("dec", "Decision", 80), ("why", "Why", 200)):
            self.tv.heading(k, text=h)
            self.tv.column(k, width=w, minwidth=30, stretch=k in ("desc", "why"))
        ttk.Style(self).configure("Viewer.Treeview", rowheight=68)
        self.tv.configure(style="Viewer.Treeview")
        ys = ttk.Scrollbar(tvf, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=ys.set)
        self.tv.pack(side="left", fill="both", expand=True)
        ys.pack(side="left", fill="y")
        for tag, colour in COLOURS.items():
            if colour:
                self.tv.tag_configure(tag, foreground=colour)
        self.tv.bind("<<TreeviewSelect>>", lambda _e: self._select_series())
        lb = ttk.Frame(left)
        lb.pack(fill="x", pady=(6, 0))
        self.b_override = ttk.Button(lb, text="Keep this series instead", command=self._override)
        self.b_override.pack(side="left")
        self.b_unoverride = ttk.Button(lb, text="Undo", command=self._unoverride)
        self.b_unoverride.pack(side="left", padx=(6, 0))

        # ---- middle: image
        mid = ttk.Frame(pane)
        pane.add(mid, weight=3)
        tb = ttk.Frame(mid)
        tb.pack(fill="x", pady=(0, 4))
        for o in pv.ORIENTATIONS:
            ttk.Radiobutton(tb, text=o, value=o, variable=self.v_orient, command=self._orient).pack(side="left", padx=(0, 6))
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(tb, text="Fit", width=4, command=lambda: self._set_zoom(1.0, reset_pan=True)).pack(side="left")
        ttk.Button(tb, text="1:1", width=4, command=self._one_to_one).pack(side="left", padx=(4, 0))
        ttk.Button(tb, text="+", width=3, command=lambda: self._zoom_step(1)).pack(side="left", padx=(4, 0))
        ttk.Button(tb, text="-", width=3, command=lambda: self._zoom_step(-1)).pack(side="left", padx=(4, 0))
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        self.b_play = ttk.Button(tb, text="Play", width=5, command=self._toggle_play)
        self.b_play.pack(side="left")
        ttk.Spinbox(tb, from_=2, to=40, textvariable=self.v_fps, width=3).pack(side="left", padx=(4, 0))
        ttk.Label(tb, text="fps").pack(side="left", padx=(2, 6))
        ttk.Checkbutton(tb, text="Invert", variable=self.v_invert, command=self._render).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(tb, text="Annotations", variable=self.v_annot, command=self._render).pack(side="left", padx=(6, 0))
        ttk.Label(tb, textvariable=self.v_readout, font=SMALL).pack(side="right")

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
        ttk.Label(row, text="wheel / arrows: slices · drag: window/level · shift-drag or right-drag: pan · cmd/ctrl-wheel: zoom",
                  foreground="#6e7781").pack(side="left")
        self.lbl_slice = ttk.Label(row, text="")
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
        self.lbl_header = ttk.Label(hdr, text="Header: before and after", font=("TkDefaultFont", 12, "bold"))
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
        for tag, colour in COLOURS.items():
            if colour:
                self.th.tag_configure(tag, foreground=colour)
        ttk.Label(right, textvariable=self.v_summary, foreground="#6e7781").pack(anchor="w", pady=(4, 0))

        ttk.Label(self, textvariable=self.v_status, padding=(10, 4)).pack(fill="x")
        self._redact_mode()

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
        for i, s in enumerate(series):
            tag = s.verdict
            dec = {"keep": "keep", "maybe": "needs a look", "drop": "drop", "review": "quarantined"}.get(s.verdict, s.verdict)
            if override and override in (s.uid, s.description):
                dec, tag = "KEEP (override)", "override"
            self.tv.insert("", "end", iid=str(i), text="", values=(s.number, s.description, s.n_images, f"{s.thickness:g}" if s.thickness else "", dec, s.reason), tags=(tag,))
        n = sum(s.n_images for s in series)
        self.v_status.set(f"{len(series)} series, {n} images. Study ID: {self._study_id()}")
        first = next((str(i) for i, s in enumerate(series) if s.verdict in ("keep", "review")), "0" if series else None)
        if first is not None:
            self.tv.selection_set(first)
            self.tv.see(first)

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
        br = (f"{s.thickness:g} mm" if s.thickness else "") + (f"  {s.kernel}" if s.kernel else "")
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
        self.drag = (e.x, e.y, self.wl)
        if self.v_redact.get():
            self._rubber = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3b30", width=2)

    def _motion(self, e) -> None:
        if not self.drag or self.view is None:
            return
        x0, y0, wl0 = self.drag
        if self.v_redact.get():
            self.canvas.coords(self._rubber, x0, y0, e.x, e.y)
        else:
            wc, ww = wl0 or (self.slice.default_window if self.slice and self.slice.default_window else (40.0, 400.0))
            self.wl = (wc + (y0 - e.y) * 2.0, max(1.0, ww + (e.x - x0) * 4.0))
            self.v_wc.set(f"{self.wl[0]:g}")
            self.v_ww.set(f"{self.wl[1]:g}")
            self._render()

    def _mouse_release(self, e) -> None:
        if not self.drag:
            return
        x0, y0, _ = self.drag
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
        self.canvas.configure(cursor="tcross" if self.v_redact.get() else "fleur")
        self._render()

    def _clear_boxes(self) -> None:
        self.boxes = []
        self._render()

    def _do_release(self, all_files: bool) -> None:
        if not (self.cur and self.cur.verdict == "review" and self.out):
            return
        files = list(self.cur.files) if all_files else [self._current_path()]
        what = f"{len(files)} file(s) of series S{self.cur.number}" if all_files else self._current_path().name
        msg = (f"Paint {len(self.boxes)} box(es) black in {what} and move it out of quarantine into the study folder?\n\n"
               "The original scan is not touched. Run the output check again afterwards.") if self.boxes else \
              (f"No boxes drawn. Release {what} from quarantine unchanged? Only do this if you have looked at it and it carries no burned-in text.")
        if not messagebox.askyesno("Release from quarantine", msg, parent=self, icon="warning", default="no"):
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
                rows = pv.header_diff(path, self._study_id(), salt, self.spec.keep_technical)
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
        p = Path(self.spec.series_pick) if self.spec.series_pick.strip() else pv.default_picks_path(self.spec.manifest)
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

    def _open_external(self) -> None:
        path = self._current_path()
        if path:
            from .ui import open_path
            open_path(path)
        else:
            messagebox.showinfo("Nothing selected", "Select a series first.", parent=self)

    def _close(self) -> None:
        self._stop_play()
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
