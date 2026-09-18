"""Profile editor dialog: pick, inspect, copy and edit de-identification profiles.

Only the options scrubdicom.profiles exposes are shown: what a profile may retain and what it replaces a few
values with. There is no per-tag list on purpose; the floor (private tags, UIDs, physicians, accession, comments,
addresses) cannot be switched off from here or anywhere else.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from . import model
from . import theme
from scrubdicom.profiles import Profile

OPTIONS = (
    ("keep_sex", "Keep sex (PatientSex)"),
    ("keep_age_5y", "Keep age, rounded down to 5 years (date of birth is still removed)"),
    ("keep_weight_height", "Keep weight and height"),
    ("shift_dates", "Keep dates, shifted by a secret per-patient offset (intervals survive; clock times kept)"),
    ("keep_manufacturer", "Keep scanner make and model"),
    ("keep_technical", "Keep kernel, scan options and full ImageType (identifies the scanner make)"),
    ("keep_institution", "Keep institution name"),
)
FLOOR = ("Always removed in every profile: private tags, original UIDs, physician and operator names, accession and order "
         "numbers, addresses, comments, other patient IDs, device serial, software versions, PACS AE titles.")


class ProfileEditor(tk.Toplevel):
    def __init__(self, app, current_path: str):
        super().__init__(app)
        self.app = app
        self.title("De-identification profiles")
        self.minsize(900, 560)
        self.geometry("980x600")
        self.transient(app)
        self.entries: list[model.ProfileEntry] = []
        self.sel: model.ProfileEntry | None = None
        self.result_path: str | None = None
        self.v_name = tk.StringVar()
        self.v_opts = {k: tk.BooleanVar() for k, _ in OPTIONS}
        self.v_pn = tk.StringVar(value="study_id")
        self.v_pn_text = tk.StringVar()
        self.v_sd = tk.StringVar()
        self.v_method = tk.StringVar()
        self.v_desc = tk.StringVar()
        self._build()
        self._reload(select_path=current_path)
        for v in list(self.v_opts.values()) + [self.v_pn, self.v_pn_text, self.v_sd, self.v_method, self.v_name]:
            v.trace_add("write", lambda *_: self._live())

    def _build(self) -> None:
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=12, pady=12)
        left = ttk.Frame(pane, padding=(0, 0, 8, 0))
        pane.add(left, weight=1)
        ttk.Label(left, text="Profiles", style="H2.TLabel").pack(anchor="w")
        self.lb = tk.Listbox(left, exportselection=False, height=14)
        theme.style_text(self.lb)
        self.lb.pack(fill="both", expand=True, pady=(4, 6))
        self.lb.bind("<<ListboxSelect>>", lambda _e: self._pick())
        b = ttk.Frame(left)
        b.pack(fill="x")
        ttk.Button(b, text="New copy", command=self._copy).pack(side="left")
        self.b_delete = ttk.Button(b, text="Delete", command=self._delete)
        self.b_delete.pack(side="left", padx=(6, 0))

        right = ttk.Frame(pane, padding=(8, 0, 0, 0))
        pane.add(right, weight=3)
        row = ttk.Frame(right)
        row.pack(fill="x")
        ttk.Label(row, text="Name").pack(side="left")
        self.e_name = ttk.Entry(row, textvariable=self.v_name, width=40)
        self.e_name.pack(side="left", padx=(8, 0))
        self.lbl_kind = ttk.Label(row, text="", style="Muted.TLabel")
        self.lbl_kind.pack(side="left", padx=(12, 0))
        ttk.Label(right, textvariable=self.v_desc, wraplength=620, style="Muted.TLabel").pack(anchor="w", pady=(4, 8))

        box = ttk.LabelFrame(right, text="What this profile keeps that the default removes", padding=8)
        box.pack(fill="x")
        self.checks = []
        for k, label in OPTIONS:
            cb = ttk.Checkbutton(box, text=label, variable=self.v_opts[k])
            cb.pack(anchor="w")
            self.checks.append(cb)

        rep = ttk.LabelFrame(right, text="Replacement values", padding=8)
        rep.pack(fill="x", pady=(8, 0))
        rep.columnconfigure(1, weight=1)
        ttk.Label(rep, text="Patient name becomes").grid(row=0, column=0, sticky="w")
        pn = ttk.Frame(rep)
        pn.grid(row=0, column=1, sticky="w")
        self.radios = []
        for val, text in (("study_id", "the study ID"), ("anonymous", "ANONYMOUS"), ("custom", "this text:")):
            r = ttk.Radiobutton(pn, text=text, value=val, variable=self.v_pn)
            r.pack(side="left", padx=(0, 10))
            self.radios.append(r)
        self.e_pn = ttk.Entry(pn, textvariable=self.v_pn_text, width=18)
        self.e_pn.pack(side="left")
        ttk.Label(rep, text="Study description becomes").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.e_sd = ttk.Entry(rep, textvariable=self.v_sd, width=40)
        self.e_sd.grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(rep, text="(blank = empty; e.g. CTCA)", style="Muted.TLabel").grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(rep, text="Method text written in the file").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.e_method = ttk.Entry(rep, textvariable=self.v_method, width=40)
        self.e_method.grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Label(rep, text="(blank = Scrub-DICOM version + what was kept)", style="Muted.TLabel").grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(6, 0))

        ttk.Label(right, text=FLOOR, wraplength=620, style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
        bb = ttk.Frame(right)
        bb.pack(fill="x", pady=(12, 0), side="bottom")
        self.b_save = ttk.Button(bb, text="Save", command=self._save)
        self.b_save.pack(side="left")
        ttk.Button(bb, text="Use this profile", command=self._use).pack(side="left", padx=(8, 0))
        ttk.Button(bb, text="Close", command=self.destroy).pack(side="right")

    # ------------------------------------------------------------------ data
    def _reload(self, select_path: str | None = None, select_key: str | None = None) -> None:
        self.entries = model.list_profiles()
        self.lb.delete(0, "end")
        for e in self.entries:
            self.lb.insert("end", e.label)
        idx = 0
        for i, e in enumerate(self.entries):
            if (select_key and e.key == select_key) or (select_path and e.path and str(e.path) == select_path):
                idx = i
        if self.entries:
            self.lb.selection_clear(0, "end")
            self.lb.selection_set(idx)
            self.lb.see(idx)
            self._pick()

    def _pick(self) -> None:
        sel = self.lb.curselection()
        if not sel:
            return
        self.sel = self.entries[sel[0]]
        p = self.sel.profile
        self._loading = True
        self.v_name.set(p.name)
        for k, _ in OPTIONS:
            self.v_opts[k].set(getattr(p, k))
        self.v_pn.set(p.patient_name)
        self.v_pn_text.set(p.patient_name_text)
        self.v_sd.set(p.study_description)
        self.v_method.set(p.method_text)
        self._loading = False
        ro = self.sel.builtin
        state = ["disabled"] if ro else ["!disabled"]
        for w in self.checks + self.radios + [self.e_name, self.e_pn, self.e_sd, self.e_method]:
            w.state(state)
        self.b_save.state(state)
        self.b_delete.state(state)
        self.lbl_kind.configure(text="built-in (read-only; make a copy to change it)" if ro else "your profile")
        self._live()

    def _form(self) -> Profile:
        return Profile(name=self.v_name.get().strip() or "Untitled", patient_name=self.v_pn.get(), patient_name_text=self.v_pn_text.get(),
                       study_description=self.v_sd.get(), method_text=self.v_method.get(), **{k: bool(self.v_opts[k].get()) for k, _ in OPTIONS})

    def _live(self) -> None:
        if getattr(self, "_loading", False):
            return
        p = self._form()
        problems = p.validate()
        self.v_desc.set((p.describe() + "  Method text: " + p.method_string(model.ENGINE_VERSION)) if not problems else "Problem: " + "; ".join(problems))

    def _copy(self) -> None:
        p = self._form()
        p.name = (self.sel.profile.name if self.sel else "Profile") + " copy"
        try:
            path = model.save_user_profile(p)
        except (ValueError, OSError) as e:
            messagebox.showerror("Cannot save", str(e), parent=self)
            return
        self._reload(select_path=str(path))

    def _save(self) -> None:
        if not self.sel or self.sel.builtin:
            return
        p = self._form()
        try:
            path = model.save_user_profile(p, self.sel.path)
            if self.sel.path and path != self.sel.path:
                model.delete_user_profile(self.sel.path)   # renamed
        except (ValueError, OSError) as e:
            messagebox.showerror("Cannot save", str(e), parent=self)
            return
        self._reload(select_path=str(path))
        self.app.v_status.set(f"Profile saved: {path.name}")

    def _delete(self) -> None:
        if not self.sel or self.sel.builtin or not self.sel.path:
            return
        if messagebox.askyesno("Delete profile", f"Delete '{self.sel.profile.name}'?", parent=self):
            model.delete_user_profile(self.sel.path)
            self._reload(select_key="builtin:blinded")

    def _use(self) -> None:
        if not self.sel:
            return
        if not self.sel.builtin:
            self._save()
        self.result_path = str(self.sel.path) if self.sel.path else ""
        self.app.v_profile.set(self.result_path)
        self.app._refresh_profile_label()
        self.destroy()
