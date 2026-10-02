"""What anonymising does to one file, in ordinary words. No Tk.

The viewer shows every tag before and after. This module turns the same comparison into the dozen lines a clinician
cares about ("Name: SMITH JOHN, now DEMO-001"; "Date of birth: removed") plus one sentence for everything else.
It only rephrases preview.header_diff's rows; it decides nothing.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

# label, DICOM keywords (first present wins; several are joined), how to show the value
FIELDS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("Name", ("PatientName",), "name"),
    ("Hospital number", ("PatientID",), "text"),
    ("NHS / other numbers", ("OtherPatientIDs",), "text"),
    ("Date of birth", ("PatientBirthDate",), "date"),
    ("Sex", ("PatientSex",), "sex"),
    ("Age", ("PatientAge",), "age"),
    ("Address", ("PatientAddress",), "text"),
    ("Scan date", ("StudyDate",), "date"),
    ("Hospital", ("InstitutionName",), "text"),
    ("Referring doctor", ("ReferringPhysicianName",), "name"),
    ("Accession number", ("AccessionNumber",), "text"),
    ("Scanner", ("Manufacturer", "ManufacturerModelName"), "text"),
    ("Comments", ("ImageComments",), "text"),
)
DUMMY_DATES = ("19000101",)
# the engine's short names for what a profile keeps (written into each file), in words for a person
KEPT_WORDS = {"sex": "sex", "age5y": "a 5-year age band", "weight": "weight and height", "dates-shifted": "shifted dates",
              "scanner": "the scanner make and model", "technical": "scanner technical details", "institution": "the hospital name"}


@dataclass
class Change:
    label: str
    before: str
    after: str
    state: str      # removed | replaced | kept


def nice_date(v: str) -> str:
    """'19610314' -> '14 Mar 1961'. Anything else is returned as it is."""
    try:
        d = _dt.datetime.strptime(str(v).strip()[:8], "%Y%m%d")
    except ValueError:
        return str(v)
    return f"{d.day} {d.strftime('%b %Y')}"


def nice(value: str, kind: str) -> str:
    v = str(value or "").strip()
    if not v:
        return ""
    if kind == "name":
        return " ".join(part for part in v.replace("^", " ").split())
    if kind == "date":
        return nice_date(v)
    if kind == "sex":
        return {"M": "male", "F": "female", "O": "other"}.get(v.upper(), v)
    if kind == "age" and len(v) == 4 and v[:3].isdigit():
        unit = {"Y": "years", "M": "months", "W": "weeks", "D": "days"}.get(v[3].upper(), "")
        return f"{int(v[:3])} {unit}".strip()
    return v


def plain_changes(rows) -> tuple[list[Change], str]:
    """(the lines worth showing, one sentence about the rest) from preview.header_diff rows."""
    top = {r.keyword: r for r in rows if r.keyword and "." not in r.path and "[" not in r.path}
    out: list[Change] = []
    shown: set[str] = set()
    for label, keywords, kind in FIELDS:
        present = [top[k] for k in keywords if k in top and str(top[k].before).strip()]
        if not present:
            continue
        shown.update(r.keyword for r in present)
        before = " ".join(nice(r.before, kind) for r in present)
        states = {r.action for r in present}
        afters = [str(r.after).strip() for r in present]
        if states == {"kept"}:
            out.append(Change(label, before, "kept", "kept"))
        elif all(not a for a in afters):
            out.append(Change(label, before, "removed", "removed"))
        elif kind == "date" and afters[0][:8] in DUMMY_DATES:
            out.append(Change(label, before, f"{nice_date(afters[0])} (a dummy date)", "replaced"))
        elif kind == "date":
            out.append(Change(label, before, f"{nice_date(afters[0])} (moved)", "replaced"))
        elif "kept" in states and len(states) > 1:
            out.append(Change(label, before, " ".join(nice(a, kind) for a in afters if a) or "removed", "replaced"))
        else:
            out.append(Change(label, before, " ".join(nice(a, kind) for a in afters if a), "replaced"))
    rest = [r for r in rows if r.action in ("removed", "changed") and r.keyword not in shown]
    private = sum(1 for r in rest if r.keyword == "private")
    uids = sum(1 for r in rest if r.keyword.endswith("UID"))
    bits = []
    if private:
        bits.append(f"{private} hidden vendor tags")
    if uids:
        bits.append("every original UID")
    more = f"{len(rest)} other fields were removed or replaced" + (", including " + " and ".join(bits) if bits else "") + "." if rest else ""
    return out, more
