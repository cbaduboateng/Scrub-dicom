"""De-identification profiles: the few knobs a user may turn, and nothing else.

The engine's tag policy (core.REMOVE / CLEAR / VENDOR_HINTS, private tags, UIDs, dates) is the floor and applies
to every profile. A profile can only *retain* attributes that DICOM PS3.15 Annex E lists as retain-able options
(patient characteristics, longitudinal temporal information with modification, device identity, institution
identity) and choose a few replacement values. It can never keep a private tag, an original UID, a physician
name, an accession number, an address or a comment.

Profiles are small JSON files. The default profile reproduces the validated v0.1 behaviour exactly.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, asdict, fields
from pathlib import Path

PATIENT_NAME_CHOICES = ("study_id", "anonymous", "custom")
MAX_METHOD_LEN = 64  # DeidentificationMethod is LO


@dataclass
class Profile:
    name: str = "Blinded read (default)"
    keep_sex: bool = False              # PatientSex
    keep_age_5y: bool = False           # PatientAge rounded down to 5 years; DOB still removed
    keep_weight_height: bool = False    # PatientWeight, PatientSize
    shift_dates: bool = False           # per-patient secret offset instead of 19000101; times kept
    keep_manufacturer: bool = False     # Manufacturer, ManufacturerModelName
    keep_technical: bool = False        # kernel, scan options, full ImageType (same as --keep-technical)
    keep_institution: bool = False      # InstitutionName
    patient_name: str = "study_id"      # study_id | anonymous | custom
    patient_name_text: str = ""         # used when patient_name == custom
    study_description: str = ""         # blank, or replacement text such as "CTCA"
    method_text: str = ""               # blank = "Scrub-DICOM <version> full blind" + what was kept

    # ------------------------------------------------------------------ derived
    def is_default(self) -> bool:
        d = Profile()
        return all(getattr(self, f.name) == getattr(d, f.name) for f in fields(self) if f.name != "name")

    def retained_remove(self) -> set[str]:
        """Keywords normally deleted that this profile keeps."""
        s: set[str] = set()
        if self.keep_sex:
            s.add("PatientSex")
        return s

    def retained_clear(self) -> set[str]:
        """Keywords normally blanked that this profile keeps."""
        s: set[str] = set()
        if self.keep_weight_height:
            s |= {"PatientWeight", "PatientSize"}
        if self.keep_manufacturer:
            s |= {"Manufacturer", "ManufacturerModelName"}
        if self.keep_institution:
            s.add("InstitutionName")
        return s

    def patient_name_for(self, study_id: str) -> str:
        if self.patient_name == "anonymous":
            return "ANONYMOUS"
        if self.patient_name == "custom" and self.patient_name_text.strip():
            return self.patient_name_text.strip()[:64]
        return study_id

    def kept_summary(self) -> list[str]:
        k = []
        if self.keep_sex:
            k.append("sex")
        if self.keep_age_5y:
            k.append("age5y")
        if self.keep_weight_height:
            k.append("weight")
        if self.shift_dates:
            k.append("dates-shifted")
        if self.keep_manufacturer:
            k.append("scanner")
        if self.keep_technical:
            k.append("technical")
        if self.keep_institution:
            k.append("institution")
        return k

    def method_string(self, version: str) -> str:
        if self.method_text.strip():
            return self.method_text.strip()[:MAX_METHOD_LEN]
        kept = self.kept_summary()
        s = f"Scrub-DICOM {version} " + ("full blind" if not kept else "kept:" + ",".join(kept))
        return s[:MAX_METHOD_LEN]

    def describe(self) -> str:
        kept = self.kept_summary()
        return "Removes everything identifying; " + ("nothing retained." if not kept else "retains " + ", ".join(kept) + ".")

    def validate(self) -> list[str]:
        p = []
        if self.patient_name not in PATIENT_NAME_CHOICES:
            p.append(f"patient_name must be one of {PATIENT_NAME_CHOICES}")
        if self.patient_name == "custom" and not self.patient_name_text.strip():
            p.append("Custom patient name text is empty")
        for label, text in (("patient name", self.patient_name_text), ("study description", self.study_description), ("method text", self.method_text)):
            if any(ord(c) < 32 or c == "\\" for c in text):
                p.append(f"The {label} contains characters DICOM does not allow")
        if len(self.study_description) > 64 or len(self.method_text) > MAX_METHOD_LEN:
            p.append("Replacement texts must be 64 characters or fewer")
        if not self.name.strip():
            p.append("Profile needs a name")
        return p

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        allowed = {f.name: f for f in fields(cls)}
        clean = {}
        for k, v in (d or {}).items():
            if k in allowed and isinstance(v, type(getattr(cls(), k))):
                clean[k] = v
        return cls(**clean)

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return path


def load_profile(path: str | Path | None) -> Profile:
    if not path:
        return Profile()
    p = Path(path)
    try:
        return Profile.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        raise SystemExit(f"Cannot read profile {p}: {e}")


BUILTIN: dict[str, Profile] = {
    "blinded": Profile(),
    "longitudinal": Profile(name="Longitudinal follow-up", keep_sex=True, keep_age_5y=True, shift_dates=True),
    "characteristics": Profile(name="Blinded read + patient characteristics", keep_sex=True, keep_age_5y=True, keep_weight_height=True),
    "technical": Profile(name="Blinded read + scanner and technical", keep_manufacturer=True, keep_technical=True),
}


# ---------------------------------------------------------------------- date shifting

def shift_days_for(salt: str, study_id: str) -> int:
    """A secret, deterministic per-patient offset in days (1..3650), backwards. Same salt + ID = same offset, so
    every file and every re-run of a patient agrees; different patients differ; nobody can recover it without the
    salt."""
    digest = hashlib.sha256(f"{salt}|dateshift|{study_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 3650 + 1


def shift_da(value: str, days: int, fallback: str) -> str:
    try:
        d = _dt.datetime.strptime(str(value).strip()[:8], "%Y%m%d").date()
    except ValueError:
        return fallback
    return (d - _dt.timedelta(days=days)).strftime("%Y%m%d")


def shift_dt(value: str, days: int, fallback: str) -> str:
    s = str(value).strip()
    if len(s) < 8:
        return fallback
    date = shift_da(s[:8], days, "")
    return date + s[8:] if date else fallback


def age_years(ds) -> int | None:
    """Age from PatientAge ('058Y', '030M') or from birth date vs study date. None if unknown."""
    a = str(ds.get("PatientAge", "") or "").strip().upper()
    if len(a) == 4 and a[:3].isdigit():
        n, unit = int(a[:3]), a[3]
        return {"Y": n, "M": n // 12, "W": n // 52, "D": n // 365}.get(unit)
    dob, ref = str(ds.get("PatientBirthDate", "") or ""), str(ds.get("StudyDate", "") or ds.get("SeriesDate", "") or "")
    try:
        b = _dt.datetime.strptime(dob[:8], "%Y%m%d").date()
        r = _dt.datetime.strptime(ref[:8], "%Y%m%d").date()
    except ValueError:
        return None
    years = r.year - b.year - ((r.month, r.day) < (b.month, b.day))
    return years if 0 <= years < 130 else None


def age_bucket_5y(years: int) -> str:
    return f"{min(years - years % 5, 120):03d}Y"
