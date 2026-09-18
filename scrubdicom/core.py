#!/usr/bin/env python3
"""
Scrub-DICOM core - batch pseudonymisation of cardiac CT DICOM studies for blinded research reads.

Non-destructive: originals are never modified; anonymised copies are written to a separate output tree.

Sub-commands
  run     anonymise an input tree (or the folders listed in a manifest) into an output tree
  verify  scan an output tree for residual identifiers and report PASS / FAIL
  thick   list (and with --fix, clear for redo) finished studies whose kept series exceed MAX_SLICE_MM

Usage examples
  # one patient folder, study ID given explicitly
  scrub-dicom run --input "/scans/P017" --output "/anon" --study-id STUDY-017

  # many patient folders, mapping file with current -> new IDs, matched on folder name
  scrub-dicom run --input "/scans/Part 1" --output "/anon" --mapping ids.xlsx --current-col "Current ID" --new-col "Study ID" --match-on folder

  # a cohort scattered over several parent folders, coronary series only, resumable, analysed series preferred
  scrub-dicom run --manifest cohort.csv --output "/anon" --resume --ctca-only --series-pick analysed_series.csv

  # the same manifest written on a Mac, run from a Windows PC where the drive is E:
  scrub-dicom run --manifest cohort.csv --output "E:\\anon" --resume --ctca-only --remap "/Volumes/Drive=E:\\"

  # check the result
  scrub-dicom verify --output "/anon" --mapping ids.xlsx --needle <surname>

Robustness built in: keeps the computer awake (macOS caffeinate / Windows power request); stops cleanly if the
output drive disappears; a study is only marked complete once fully written, so re-running with --resume redoes
anything half-finished; a watchdog names the file if a failing drive hangs; files listed in skip_files.txt next
to the manifest are never opened; per-study series decisions are logged as they happen.

Requires: pydicom >= 2.4. openpyxl only if the mapping file is .xlsx.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

try:
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.sequence import Sequence
    from pydicom.uid import ExplicitVRLittleEndian, PYDICOM_IMPLEMENTATION_UID
except ImportError:
    sys.exit("pydicom is not installed. Run:  pip install pydicom")

VERSION = "0.3.0"
DUMMY_DATE = "19000101"
DUMMY_TIME = "111111.111111"
DUMMY_DATETIME = "19000101111111.111111"
DEID_METHOD = f"Scrub-DICOM {VERSION} full blind"  # LO, max 64 chars

# ---------------------------------------------------------------------------
# Tag policy. Keywords are pydicom keywords. See docs/tag_policy.md for the rationale.
# ---------------------------------------------------------------------------

# Removed entirely (element deleted). Absence is safer than a blank for these.
REMOVE = {
    "PatientBirthDate", "PatientBirthTime", "PatientSex", "PatientAge", "PatientBirthName",
    "PatientMotherBirthName", "OtherPatientIDs", "OtherPatientNames", "OtherPatientIDsSequence",
    "PatientAddress", "PatientTelephoneNumbers", "PatientTelecomInformation", "EthnicGroup",
    "Occupation", "PatientReligiousPreference", "MilitaryRank", "BranchOfService",
    "CountryOfResidence", "RegionOfResidence", "MedicalRecordLocator", "ResponsiblePerson",
    "ResponsibleOrganization", "PatientInsurancePlanCodeSequence", "PatientPrimaryLanguageCodeSequence",
    "AdditionalPatientHistory", "PatientComments", "StudyComments", "ImageComments",
    "IssuerOfPatientID", "IssuerOfPatientIDQualifiersSequence", "TypeOfPatientID",
    "InstitutionAddress", "InstitutionCodeSequence", "InstitutionalDepartmentName",
    "DeviceSerialNumber", "SoftwareVersions", "DeviceUID", "GantryID", "PlateID", "DetectorID",
    "RequestAttributesSequence", "ReferencedPatientSequence", "ReferencedStudySequence",
    "ReferencedPerformedProcedureStepSequence", "PerformedProcedureStepID",
    "PerformedProcedureStepDescription", "PerformedProcedureStepStartDate", "PerformedProcedureStepStartTime",
    "ScheduledProcedureStepID", "RequestedProcedureID", "RequestedProcedureDescription",
    "RequestedProcedureCodeSequence", "ProcedureCodeSequence", "ReasonForStudy",
    "ReasonForTheRequestedProcedure", "RequestingPhysician", "RequestingService",
    "PlacerOrderNumberImagingServiceRequest", "FillerOrderNumberImagingServiceRequest",
    "ScheduledProcedureStepDescription", "ScheduledPerformingPhysicianName",
    "CurrentPatientLocation", "PatientInstitutionResidence", "AdmissionID", "AdmittingDiagnosesDescription",
    "ClinicalTrialSponsorName", "ClinicalTrialProtocolID", "ClinicalTrialSiteID", "ClinicalTrialSiteName",
    "ClinicalTrialSubjectID", "ClinicalTrialCoordinatingCenterName",
    "OriginalAttributesSequence", "ContributingEquipmentSequence", "ReferencedRequestSequence",
    "IdentifyingComments", "DerivationDescription", "AcquisitionDeviceProcessingDescription",
    "ContentSequence",  # SR content; SR objects are quarantined anyway
}

# Set to an empty value (element kept, value cleared). Preserves Type 2 conformance.
CLEAR = {
    "AccessionNumber", "StudyID", "InstitutionName", "StationName", "Manufacturer",
    "ManufacturerModelName", "ReferringPhysicianName", "ReferringPhysicianIdentificationSequence",
    "PhysiciansOfRecord", "PerformingPhysicianName", "NameOfPhysiciansReadingStudy", "OperatorsName",
    "StudyDescription", "SeriesDescription", "ProtocolName", "PerformedProtocolCodeSequence",
    "PatientWeight", "PatientSize", "PregnancyStatus",
}

# Standard DICOM UID prefix that must never be replaced (SOP classes, transfer syntaxes, etc.)
STANDARD_UID_PREFIX = "1.2.840.10008."
UID_KEEP = {"SOPClassUID", "TransferSyntaxUID", "MediaStorageSOPClassUID", "ImplementationClassUID",
            "ReferencedSOPClassUID", "CodingSchemeUID", "MappingResourceUID", "ContextGroupExtensionCreatorUID"}

# Objects routed to _review/ instead of the main output: no pixel-level anonymisation is attempted.
REVIEW_SOP_CLASSES = {
    "1.2.840.10008.5.1.4.1.1.7",        # Secondary Capture Image Storage
    "1.2.840.10008.5.1.4.1.1.7.1", "1.2.840.10008.5.1.4.1.1.7.2",
    "1.2.840.10008.5.1.4.1.1.7.3", "1.2.840.10008.5.1.4.1.1.7.4",
    "1.2.840.10008.5.1.4.1.1.88.67",    # X-Ray Radiation Dose SR
    "1.2.840.10008.5.1.4.1.1.88.11", "1.2.840.10008.5.1.4.1.1.88.22",
    "1.2.840.10008.5.1.4.1.1.88.33", "1.2.840.10008.5.1.4.1.1.88.59",  # SR / Key Object
    "1.2.840.10008.5.1.4.1.1.104.1",    # Encapsulated PDF
    "1.2.840.10008.5.1.4.1.1.11.1",     # Grayscale Softcopy Presentation State
}
REVIEW_MODALITIES = {"SR", "PR", "KO", "DOC", "OT"}

# Technical fields whose vocabulary names the vendor (B26f, WEDGE_2, PULS_EC, CT_SOM5...). Cleared by
# default because a reader who recognises the scanner can guess the centre; --keep-technical retains them.
VENDOR_HINTS = {"ConvolutionKernel", "ScanOptions", "FilterType", "ExposureModulationType", "FilterMaterial",
                "ReconstructionAlgorithm", "AcquisitionProtocolName", "ImageFilter", "PerformedProtocolCodeSequence"}


# ---------------------------------------------------------------------------
# CTCA series selection (--ctca-only). Header-only, vendor-agnostic. Keeps the coronary reconstructions:
# ORIGINAL/PRIMARY CT, slice <= 1.0 mm, >= 100 images (or a multi-frame volume), reconstruction FOV <= 260 mm,
# soft/vascular kernel, and either a contrast agent, a cardiac phase, or a CARDIAC_CTA image type. Drops
# localisers, calcium score, whole-chest recons, lung/sharp kernels, MPRs, dose reports.
# ---------------------------------------------------------------------------
MAX_SLICE_MM = 0.8   # keep only reconstructions at 0.75 mm or thinner (Siemens 0.75, GE/Canon 0.5/0.625); 1 mm+ is not a coronary read
SERIES_TAGS = ["SeriesInstanceUID", "SeriesNumber", "SeriesDescription", "Modality", "SOPClassUID", "ImageType",
               "SliceThickness", "ContrastBolusAgent", "ConvolutionKernel", "ReconstructionDiameter", "NumberOfFrames",
               "NominalPercentageOfCardiacPhase", "SharedFunctionalGroupsSequence", "PerFrameFunctionalGroupsSequence"]
SHARP_KERNELS = ("I70", "B70", "B60", "I50", "FC51", "FC52", "FC85", "FC86", "BONE", "LUNG", "EDGE", "Br59", "Qr36",
                 "Qr40", "Sa36", "Bl64", "Br64", "B80", "I80", "Br69", "Hr60", "Hr64", "Hr68", "Qr59")
DROP_WORDS = ("topogram", "scout", "localizer", "localiser", "surview", "lung", "bone", "calcium", "cascore", "ca score",
              "ca-score", "score", "non contrast", "noncontrast", "mpr", "snapshot", "cpr", "curved", "mip", "dose", "report",
              "monitoring", "bolus track", "test bolus", "premonitor", "timing", "pulmonary", "ctpa", "abdomen", "pelvis",
              "delayed", "late phase", "secondary", "capture", "aorta", "aortic", "thorax", "reformat", "sagittal",
              "coronal", "3d", "fai", "caristo", "epicardial")


def series_info(ds):
    """Pull thickness / kernel / FOV / phase, looking inside enhanced multi-frame functional groups if needed."""
    thick, kern, fov, phase = ds.get("SliceThickness"), ds.get("ConvolutionKernel"), ds.get("ReconstructionDiameter"), ds.get("NominalPercentageOfCardiacPhase")
    sfg = ds.get("SharedFunctionalGroupsSequence")
    if sfg:
        for getter in (lambda: sfg[0].PixelMeasuresSequence[0].SliceThickness,):
            try:
                thick = thick or getter()
            except Exception:
                pass
        try:
            kern = kern or sfg[0].CTReconstructionSequence[0].ConvolutionKernel
        except Exception:
            pass
        try:
            fov = fov or sfg[0].CTReconstructionSequence[0].ReconstructionFieldOfView[0]
        except Exception:
            pass
        try:
            phase = phase or ds.PerFrameFunctionalGroupsSequence[0].CardiacSynchronizationSequence[0].NominalPercentageOfCardiacPhase
        except Exception:
            pass
    if isinstance(kern, (list, tuple)) or kern.__class__.__name__ == "MultiValue":
        kern = " ".join(str(k) for k in kern)
    try:
        thick = float(thick) if thick not in (None, "") else None
    except (TypeError, ValueError):
        thick = None
    try:
        fov = float(fov) if fov not in (None, "") else None
    except (TypeError, ValueError):
        fov = None
    return thick, str(kern or ""), fov, phase


def classify_series(ds, n_files: int):
    """Return (keep: bool, reason: str) for one representative header of a series."""
    thick, kern, fov, phase = series_info(ds)
    itype = "\\".join(str(x) for x in (ds.get("ImageType", []) or [])).upper()
    desc = str(ds.get("SeriesDescription", "") or "")
    contrast = str(ds.get("ContrastBolusAgent", "") or "")
    frames = int(ds.get("NumberOfFrames", 0) or 0)
    sop = str(ds.get("SOPClassUID", ""))
    why = []
    if str(ds.get("Modality", "")) != "CT" or sop in REVIEW_SOP_CLASSES or sop.startswith("1.2.840.10008.5.1.4.1.1.88"):
        why.append(f"not a CT image ({ds.get('Modality', '?')})")
    if "LOCALIZER" in itype or "SECONDARY" in itype or "DERIVED" in itype or "MPR" in itype:
        why.append("localiser/derived")
    if thick is not None and thick > MAX_SLICE_MM:
        why.append(f"{thick:g} mm slices")
    if n_files < 100 and frames < 100:
        why.append(f"only {n_files} images")
    if fov and fov > 260:
        why.append(f"FOV {fov:.0f} mm (chest recon)")
    if any(k.lower() in kern.lower() for k in SHARP_KERNELS):
        why.append(f"kernel {kern}")
    # GE names its prospectively gated CTCA acquisition "SnapShot Pulse/Segment/Burst/Freeze";
    # that is the coronary series itself, not a syngo.via MPR snapshot
    dl = re.sub(r"snap\s*shot\s*(pulse|segment|burst|freeze)", "ge_gated_cta", desc.lower())
    hits = [w for w in DROP_WORDS if w in dl]
    if hits:
        why.append(f"description '{hits[0]}'")
    if why:
        return "drop", "; ".join(why)
    label = f"coronary recon: {thick:g} mm" + (f", FOV {fov:.0f}" if fov else "") + (f", {kern}" if kern else "")
    if not contrast and phase is None and "CARDIAC_CTA" not in itype and not re.search(r"cta|coro|angio", desc, re.I):
        return "maybe", label + " - no contrast agent / cardiac phase in header"
    return "keep", label


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LOG_FILE = None


def log(msg: str) -> None:
    print(msg, flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(msg + "\n")
        except OSError:
            pass


def safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(s)).strip("_")
    return s or "unnamed"


def hashed_uid(original: str, salt: str) -> str:
    """Deterministic replacement UID in the 2.25.<uuid-int> form. Same input + salt -> same output,
    so all files of a study keep a consistent StudyInstanceUID and cross-references still resolve."""
    digest = hashlib.sha256((salt + "|" + str(original)).encode()).digest()
    return "2.25." + str(int.from_bytes(digest[:16], "big"))


def load_mapping(path: str, current_col: str | None, new_col: str | None, sheet: str | None = None) -> dict[str, str]:
    """Read a CSV or XLSX mapping of current ID -> new study ID. Column names are auto-detected
    unless given. Keys are normalised (stripped, upper-cased) to survive spreadsheet sloppiness."""
    p = Path(path)
    rows: list[dict] = []
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            sys.exit("openpyxl is needed for .xlsx mapping files: pip install openpyxl")
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.active
        it = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(it)]
        for r in it:
            if r is None or all(v is None for v in r):
                continue
            rows.append({header[i]: ("" if v is None else str(v).strip()) for i, v in enumerate(r) if i < len(header)})
    else:
        with open(p, newline="", encoding="utf-8-sig") as fh:
            rows = [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)]
    if not rows:
        sys.exit(f"Mapping file {path} has no rows")
    cols = list(rows[0].keys())

    def pick(explicit, candidates, label, fallback_index):
        if explicit:
            if explicit not in cols:
                sys.exit(f"Column '{explicit}' not found in mapping. Columns are: {cols}")
            return explicit
        for c in cols:
            if any(k in c.lower() for k in candidates):
                return c
        if len(cols) == 2:  # two columns: assume current first, new second
            return cols[fallback_index]
        sys.exit(f"Could not guess the {label} column from {cols}; pass --current-col / --new-col")

    cur = pick(current_col, ("current", "old", "orig", "orfan", "mrn", "hospital", "source"), "current-ID", 0)
    new = pick(new_col, ("new", "prefer", "cbb", "study", "anon", "pseud", "target"), "new-ID", 1)
    if cur == new:
        sys.exit(f"Current and new columns both resolved to '{cur}'; pass them explicitly")
    mapping = {}
    for r in rows:
        k, v = r.get(cur, ""), r.get(new, "")
        if k and v:
            mapping[k.upper()] = v
    log(f"Mapping loaded: {len(mapping)} entries ({cur!r} -> {new!r})")
    return mapping


def is_review_object(ds: Dataset) -> str | None:
    """Return a reason string if this object should be quarantined for manual review."""
    sop = str(ds.get("SOPClassUID", ""))
    if sop in REVIEW_SOP_CLASSES:
        return f"SOP class {pydicom.uid.UID(sop).name if sop else sop}"
    if str(ds.get("Modality", "")) in REVIEW_MODALITIES:
        return f"Modality {ds.Modality}"
    if str(ds.get("BurnedInAnnotation", "")).upper() == "YES":
        return "BurnedInAnnotation=YES"
    return None


# ---------------------------------------------------------------------------
# Core anonymisation of one dataset
# ---------------------------------------------------------------------------

def _walk(ds: Dataset, salt: str, stats: dict) -> None:
    """Recursively apply date/time dummying and UID hashing to every element, including inside sequences."""
    for elem in list(ds):
        if elem.tag.is_private:
            continue  # removed wholesale afterwards
        vr = elem.VR
        if vr == "SQ":
            for item in elem.value:
                _walk(item, salt, stats)
        elif vr == "DA":
            elem.value = DUMMY_DATE if elem.value else elem.value
            stats["dates"] += 1
        elif vr == "TM":
            elem.value = DUMMY_TIME if elem.value else elem.value
            stats["dates"] += 1
        elif vr == "DT":
            elem.value = DUMMY_DATETIME if elem.value else elem.value
            stats["dates"] += 1
        elif vr == "UI":
            kw = elem.keyword
            if kw in UID_KEEP:
                continue
            vals = elem.value if elem.VM > 1 else [elem.value]
            new = [v if str(v).startswith(STANDARD_UID_PREFIX) or not v else hashed_uid(str(v), salt) for v in vals]
            elem.value = new if elem.VM > 1 else new[0]
            stats["uids"] += 1


def anonymise_dataset(ds: Dataset, study_id: str, salt: str, keep_technical: bool = False) -> dict:
    stats = {"dates": 0, "uids": 0, "removed": 0, "cleared": 0, "private": 0}

    # 1. private tags: vendor and PACS blocks are where identifiers hide (referring clinician, true
    #    study datetime, age in days were all found in ELSCINT1 blocks of a previously "anonymised" file)
    before = len(ds)
    ds.remove_private_tags()
    stats["private"] = before - len(ds)

    # 2. explicit removals and clears
    for kw in REMOVE:
        if kw in ds:
            del ds[kw]
            stats["removed"] += 1
    for kw in CLEAR:
        if kw in ds:
            elem = ds[kw]
            if elem.VR == "SQ":
                elem.value = []
            elif elem.VR in ("US", "SS", "UL", "SL", "FL", "FD", "AT", "OB", "OW"):
                del ds[kw]  # numeric VRs cannot hold an empty string
            else:
                elem.value = ""
            stats["cleared"] += 1

    if not keep_technical:
        for kw in VENDOR_HINTS:
            if kw in ds:
                del ds[kw]
        # ImageType values 1-3 are standard (ORIGINAL/PRIMARY/AXIAL); value 4+ is vendor free text
        if "ImageType" in ds and ds.ImageType and len(ds.ImageType) > 3:
            ds.ImageType = list(ds.ImageType)[:3]

    # 3. dates, times, UIDs everywhere (top level and inside sequences)
    _walk(ds, salt, stats)

    # 4. identity
    ds.PatientName = study_id
    ds.PatientID = study_id
    ds.PatientIdentityRemoved = "YES"
    ds.DeidentificationMethod = DEID_METHOD
    ds.BurnedInAnnotation = "NO"

    # 5. file meta: must match the new SOP Instance UID; strip AE titles that name the sending PACS
    fm = ds.file_meta if hasattr(ds, "file_meta") else FileMetaDataset()
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.MediaStorageSOPClassUID = ds.SOPClassUID
    for kw in ("SourceApplicationEntityTitle", "SendingApplicationEntityTitle",
               "ReceivingApplicationEntityTitle", "PrivateInformationCreatorUID", "PrivateInformation"):
        if kw in fm:
            del fm[kw]
    fm.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
    fm.ImplementationVersionName = f"DCMPSEUD_{VERSION.replace('.', '')}"
    ds.file_meta = fm
    return stats


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

SKIP_FILES: set[str] = set()   # exact source paths never to open (files that hang a failing drive); see load_skip_files
SKIPPED_HITS: list[str] = []


def load_skip_files(manifest_path: str) -> None:
    """Read skip_files.txt (one full path per line, # comments allowed) from the manifest's folder, if present."""
    p = Path(manifest_path).resolve().parent / "skip_files.txt"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                SKIP_FILES.add(line.rstrip("/"))
        if SKIP_FILES:
            log(f"Skipping {len(SKIP_FILES)} file(s) listed in {p.name}")


def iter_dicom_files(root: Path, skip: Path | None):
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if skip and (d == skip or skip in d.parents):
            dirnames[:] = []
            continue
        dirnames[:] = [x for x in dirnames if not x.startswith(("_review", "_logs", "_to_delete", "."))]
        for fn in sorted(filenames):
            if fn.startswith(".") or fn.upper() == "DICOMDIR" or fn.lower().endswith((".png", ".jpg", ".txt", ".csv", ".xlsx", ".json")):
                continue
            full = d / fn
            if SKIP_FILES and ({str(full), str(full.resolve()), str(full).replace("\\", "/"), str(full.resolve()).replace("\\", "/"),
                                str(full).replace("\\\\?\\", ""), str(full.resolve()).replace("\\\\?\\", "")} & SKIP_FILES):
                if str(full) not in SKIPPED_HITS:
                    SKIPPED_HITS.append(str(full))
                continue
            yield full


def source_key(path: Path, ds: Dataset, root: Path, match_on: str) -> str:
    if match_on == "folder":
        rel = path.relative_to(root)
        return rel.parts[0] if len(rel.parts) > 1 else root.name
    return str(ds.get("PatientID", "")).strip()


def win_path(p: str | Path) -> Path:
    """On Windows, use the extended-length form so long paths work and folder names with a trailing space
    (legal on macOS, e.g. 'Miscellaneous ') are not silently stripped by the Win32 layer."""
    s = str(p)
    if os.name == "nt" and not s.startswith("\\\\?\\") and re.match(r"^[A-Za-z]:\\", s):
        return Path("\\\\?\\" + s)
    return Path(s)


def remap_path(s: str, remap: str | None) -> Path:
    """Apply an optional 'OLDPREFIX=NEWPREFIX' rewrite (e.g. /Volumes/Mstr_SCAD=E:\\) so a manifest written on a
    Mac can drive the same job from a Windows PC with the drive at another mount point."""
    if remap and "=" in remap:
        old, new = remap.split("=", 1)
        old = old.rstrip("/\\")
        if s == old or s.startswith(old + "/") or s.startswith(old + "\\"):
            rest = s[len(old):].lstrip("/\\")
            new = new.rstrip("/\\") + ("\\" if os.name == "nt" else "/")
            s = new + (rest.replace("/", "\\") if os.name == "nt" else rest)
    return win_path(s)


def load_manifest(path: str, remap: str | None = None) -> list[tuple[Path, str]]:
    """CSV with columns source_folder, study_id: one patient folder per row, each given its own study ID.
    Used when the scans for one cohort are scattered over several parent folders."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    pairs = [(remap_path(r["source_folder"].strip(), remap), r["study_id"].strip()) for r in rows if r.get("source_folder") and r.get("study_id")]
    if not pairs:
        sys.exit("Manifest has no usable rows (needs columns source_folder, study_id)")
    return pairs


def find_folder(folder: Path) -> Path:
    """Return the first existing variant of a manifest folder path. Windows may present a macOS folder named
    'Miscellaneous ' (trailing space) as 'Miscellaneous', with or without the \\\\?\\ prefix, so try each."""
    if folder.is_dir():
        return folder
    s = str(folder)
    variants = []
    bare = s[4:] if s.startswith("\\\\?\\") else s
    variants.append(bare)
    sep = "\\" if os.name == "nt" else "/"
    parts = bare.split(sep)
    stripped = sep.join(p.rstrip(" .") if i > 0 else p for i, p in enumerate(parts))
    variants += [stripped, "\\\\?\\" + stripped] if os.name == "nt" else [stripped]
    for v in variants:
        try:
            if Path(v).is_dir():
                return Path(v)
        except (OSError, ValueError):
            pass
    return folder


_CAFFEINATE = None


def keep_awake() -> None:
    """Stop the computer (and on a Mac the disk) sleeping while a run is in progress. Built in, so it works however
    the tool is launched: Windows uses SetThreadExecutionState; macOS spawns `caffeinate -dims` tied to this process,
    which exits with it. Linux is left to the user's power settings."""
    global _CAFFEINATE
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)  # CONTINUOUS|SYSTEM|DISPLAY
        elif sys.platform == "darwin" and _CAFFEINATE is None:
            import subprocess
            _CAFFEINATE = subprocess.Popen(["caffeinate", "-dims", "-w", str(os.getpid())],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class Watchdog:
    """Background thread that shouts if no file has been processed for a while: a hung USB drive blocks the read
    forever without an error, and this at least names the file so the study can be excluded."""

    def __init__(self, quiet_s: int = 120):
        import threading
        self.quiet_s, self.path, self.t = quiet_s, "", time.time()
        self._stop = threading.Event()
        self._warned = False
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()

    def touch(self, path) -> None:
        self.path, self.t, self._warned = str(path), time.time(), False

    def _run(self) -> None:
        while not self._stop.wait(15):
            if self.path and not self._warned and time.time() - self.t > self.quiet_s:
                self._warned = True
                log(f"\n** No progress for {int(time.time() - self.t)} s while reading:\n   {self.path}\n"
                    f"   The drive has probably hung on this file. Press Ctrl+C, reconnect the drive and re-run "
                    f"(--resume). If it hangs on the same study again, remove that study from the manifest. **")

    def stop(self) -> None:
        self._stop.set()


def volume_present(out: Path) -> bool:
    """True while the drive holding the output folder is still mounted."""
    s = str(out)
    if s.startswith("/Volumes/") and len(out.parts) >= 3:
        return Path(*out.parts[:3]).exists()
    if out.anchor:
        return Path(out.anchor).exists() and out.exists()
    return out.exists()


def cmd_run(a: argparse.Namespace) -> int:
    if a.manifest:
        return run_manifest(a)
    if not a.input:
        sys.exit("Give --input or --manifest")
    inp, out = Path(a.input).resolve(), Path(a.output).resolve()
    keep_awake()
    if not inp.is_dir():
        sys.exit(f"Input folder not found: {inp}")
    if out == inp or inp in out.parents:
        log(f"Note: output {out} is inside input; it will be skipped while scanning.")
    if not a.dry_run:
        out.mkdir(parents=True, exist_ok=True)
        (out / "_logs").mkdir(exist_ok=True)
    logs = out / "_logs"

    mapping: dict[str, str] = {}
    if a.study_id:
        pass
    elif a.mapping:
        mapping = load_mapping(a.mapping, a.current_col, a.new_col, a.sheet)
    else:
        sys.exit("Give either --study-id (single patient folder) or --mapping (many patients)")

    # UID salt persists in the output tree so re-runs produce identical UIDs.
    salt_file = out / "_logs" / "uid_salt.txt"
    if salt_file.exists():
        salt = salt_file.read_text().strip()
    else:
        salt = a.salt or hashlib.sha256(os.urandom(32)).hexdigest()
        if not a.dry_run:
            salt_file.write_text(salt)

    run_ts = time.strftime("%Y%m%d_%H%M%S")
    per_study = defaultdict(lambda: {"files": 0, "series": set(), "review": 0, "errors": 0,
                                     "source_keys": set(), "orig_patient_ids": set(), "orig_names": set(),
                                     "orig_study_dates": set(), "orig_sex": set(), "orig_dob": set(),
                                     "orig_series_desc": set(), "orig_manufacturer": set()})
    unmapped = defaultdict(int)
    skipped_nondicom = 0
    file_rows = []
    counter = defaultdict(int)
    t0 = time.time()

    for path in iter_dicom_files(inp, out if inp in out.parents or out == inp else None):
        try:
            ds = pydicom.dcmread(str(path), force=True)
        except Exception:
            skipped_nondicom += 1
            continue
        if "SOPClassUID" not in ds:
            skipped_nondicom += 1
            continue

        key = a.study_id and "*" or source_key(path, ds, inp, a.match_on)
        if a.study_id:
            study_id = a.study_id
        else:
            study_id = mapping.get(key.upper())
            if not study_id:
                unmapped[key] += 1
                continue  # never copy an unmapped file through

        rec = per_study[study_id]
        rec["source_keys"].add(key)
        rec["orig_patient_ids"].add(str(ds.get("PatientID", "")))
        rec["orig_names"].add(str(ds.get("PatientName", "")))
        rec["orig_study_dates"].add(str(ds.get("StudyDate", "")))
        rec["orig_sex"].add(str(ds.get("PatientSex", "")))
        rec["orig_dob"].add(str(ds.get("PatientBirthDate", "")))
        rec["orig_series_desc"].add(f"{ds.get('SeriesNumber', '')}: {ds.get('SeriesDescription', '')}")
        rec["orig_manufacturer"].add(f"{ds.get('Manufacturer', '')} {ds.get('ManufacturerModelName', '')}".strip())

        reason = is_review_object(ds)
        series_no = str(ds.get("SeriesNumber", "0"))
        inst_no = str(ds.get("InstanceNumber", ""))
        try:
            stats = anonymise_dataset(ds, study_id, salt, a.keep_technical)
        except Exception as e:
            rec["errors"] += 1
            file_rows.append([study_id, str(path), "", "ERROR", str(e)])
            log(f"ERROR {path}: {e}")
            continue

        if reason:
            dest_dir = out / "_review" / safe_name(study_id)
            rec["review"] += 1
        elif a.flat:
            dest_dir = out / safe_name(study_id)
        else:
            dest_dir = out / safe_name(study_id) / f"S{int(float(series_no or 0)):03d}"
        counter[study_id] += 1
        n = counter[study_id]
        if inst_no:
            fname = f"{safe_name(study_id)}_S{int(float(series_no or 0)):03d}_I{int(float(inst_no)):05d}.dcm"
        else:
            fname = f"{safe_name(study_id)}_{n:06d}.dcm"
        dest = dest_dir / fname
        if dest.exists():
            dest = dest_dir / f"{dest.stem}_{n}.dcm"
        if not a.dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            ds.save_as(str(dest), write_like_original=False)
        rec["files"] += 1
        rec["series"].add(series_no)
        file_rows.append([study_id, str(path), str(dest), "REVIEW" if reason else "OK", reason or ""])
        if rec["files"] % 250 == 0:
            log(f"  {study_id}: {rec['files']} files...")

    # mapping rows whose scans were never seen: tells the user which patients are still missing from disk
    seen_keys = {k.upper() for r in per_study.values() for k in r["source_keys"]}
    missing = sorted(k for k in mapping if k not in seen_keys) if mapping else []

    # ---- logs -------------------------------------------------------------
    if a.dry_run:
        log("")
        log(f"DRY RUN: {len(per_study)} studies, {sum(r['files'] for r in per_study.values())} files would be written")
        for sid, r in sorted(per_study.items()):
            log(f"  {sid} <- {';'.join(sorted(r['source_keys']))}: {r['files']} files, {len(r['series'])} series"
                + (f", {r['review']} to _review" if r["review"] else ""))
        if unmapped:
            log(f"  UNMAPPED (would be skipped): {dict(unmapped)}")
        if missing:
            log(f"  Mapping rows with no scans found on disk ({len(missing)}): {', '.join(missing[:20])}{' ...' if len(missing) > 20 else ''}")
        if skipped_nondicom:
            log(f"  {skipped_nondicom} non-DICOM files ignored")
        return 0

    with open(logs / f"files_{run_ts}.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["study_id", "source_path", "output_path", "status", "note"])
        w.writerows(file_rows)

    # The linkage log is the ONLY place original identifiers survive. Keep it away from the anonymised scans.
    link_path = Path(a.linkage_log) if a.linkage_log else logs / f"LINKAGE_{run_ts}_CONFIDENTIAL.csv"
    with open(link_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["study_id", "source_key", "original_patient_id", "original_name", "original_study_date",
                    "original_sex", "original_dob", "original_manufacturer", "original_series_descriptions",
                    "files", "series", "review_files", "errors"])
        for sid, r in sorted(per_study.items()):
            w.writerow([sid, ";".join(sorted(r["source_keys"])), ";".join(sorted(r["orig_patient_ids"])),
                        ";".join(sorted(r["orig_names"])), ";".join(sorted(r["orig_study_dates"])),
                        ";".join(sorted(r["orig_sex"])), ";".join(sorted(r["orig_dob"])),
                        ";".join(sorted(r["orig_manufacturer"])), " | ".join(sorted(r["orig_series_desc"])),
                        r["files"], len(r["series"]), r["review"], r["errors"]])

    if unmapped or missing:
        with open(logs / f"unmapped_{run_ts}.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["source_key", "files_skipped", "note"])
            w.writerows([k, n, "scans on disk but no mapping row"] for k, n in sorted(unmapped.items()))
            w.writerows([k, 0, "mapping row but no scans found on disk"] for k in missing)

    total = sum(r["files"] for r in per_study.values())
    log("")
    log(f"Done in {time.time() - t0:.0f}s: {len(per_study)} studies, {total} files written")
    for sid, r in sorted(per_study.items()):
        log(f"  {sid}: {r['files']} files, {len(r['series'])} series"
            + (f", {r['review']} sent to _review" if r["review"] else "")
            + (f", {r['errors']} ERRORS" if r["errors"] else ""))
    if unmapped:
        log(f"  UNMAPPED (skipped, not copied): {dict(unmapped)}  -> see _logs/unmapped_{run_ts}.csv")
    if missing:
        log(f"  Mapping rows with no scans found on disk ({len(missing)}): {', '.join(missing[:20])}"
            f"{' ...' if len(missing) > 20 else ''}  -> see _logs/unmapped_{run_ts}.csv")
    if skipped_nondicom:
        log(f"  {skipped_nondicom} non-DICOM files ignored")
    log(f"  Linkage log (confidential, move it out of the output tree): {link_path}")
    return 1 if any(r["errors"] for r in per_study.values()) else 0


def load_series_picks(path: str) -> dict[str, list[dict]]:
    """CSV with study_id, series_uid, series_description: the series previously analysed (e.g. FAI) per study."""
    picks = defaultdict(list)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r.get("study_id"):
                picks[r["study_id"].strip()].append({"uid": (r.get("series_uid") or "").strip(),
                                                     "desc": (r.get("series_description") or "").strip()})
    return picks


def norm_desc(d: str) -> str:
    return re.sub(r"\s+", " ", str(d or "")).strip().lower()


def run_manifest(a: argparse.Namespace) -> int:
    """Process each (folder, study_id) pair of a manifest with the single-patient logic, sharing one output
    tree, one salt and one linkage log. Folders that do not exist are reported, not fatal."""
    pairs = load_manifest(a.manifest, getattr(a, "remap", None))
    load_skip_files(a.manifest)
    out = win_path(Path(a.output).resolve())
    keep_awake()
    if not a.dry_run:
        (out / "_logs").mkdir(parents=True, exist_ok=True)
    salt_file = out / "_logs" / "uid_salt.txt"
    if salt_file.exists():
        salt = salt_file.read_text().strip()
    else:
        salt = a.salt or hashlib.sha256(os.urandom(32)).hexdigest()
        if not a.dry_run:
            salt_file.write_text(salt)
    run_ts = time.strftime("%Y%m%d_%H%M%S")
    summary, link_rows, file_rows, missing, series_rows, no_ctca, check_studies = [], [], [], [], [], [], []
    series_log = out / "_logs" / f"series_{run_ts}.csv"
    picks = load_series_picks(a.series_pick) if a.series_pick else {}
    pick_stats = {"uid": 0, "desc": 0, "unmatched": 0}
    unmatched_picks = []
    skipped = 0
    t0 = time.time()
    drive_lost = False
    wd = Watchdog()
    for n, (folder, study_id) in enumerate(pairs, 1):
        # if the output volume has vanished (external drive unplugged / asleep), stop rather than
        # racing through the rest of the manifest reporting every folder as missing
        if not volume_present(out):
            log(f"\n** Output location {out} is no longer reachable (drive disconnected?). Stopping at study {n}. "
                f"Reconnect the drive and re-run with --resume. **")
            drive_lost = True
            break
        folder = find_folder(folder)
        if not folder.is_dir():
            missing.append((str(folder), study_id))
            log(f"[{n}/{len(pairs)}] {study_id}: folder not found: {folder}")
            skipped += 1
            continue
        study_out = out / safe_name(study_id)
        marker = study_out / (".complete_ctca" if a.ctca_only else ".complete")
        if a.resume and not a.dry_run and marker.exists():
            log(f"[{n}/{len(pairs)}] {study_id}: already complete, skipped (--resume)")
            skipped += 1
            continue
        if not a.dry_run and study_out.is_dir():
            # a folder without the marker is a half-finished run: start it again from scratch
            import shutil
            shutil.rmtree(study_out, ignore_errors=True)
            shutil.rmtree(out / "_review" / safe_name(study_id), ignore_errors=True)
        rec = {"files": 0, "series": set(), "review": 0, "errors": 0, "orig_patient_ids": set(), "orig_names": set(),
               "orig_study_dates": set(), "orig_sex": set(), "orig_dob": set(), "orig_series_desc": set(),
               "orig_manufacturer": set(), "nondicom": 0, "dropped": 0}
        counter = 0
        keep_uids = None
        pick_note = ""
        if a.ctca_only:
            # pre-pass: group files by series, classify each series from one header
            groups = defaultdict(list)
            heads = {}
            for path in iter_dicom_files(folder, None):
                wd.touch(path)
                try:
                    h = pydicom.dcmread(str(path), stop_before_pixels=True, specific_tags=SERIES_TAGS, force=True)
                except Exception:
                    continue
                if "SOPClassUID" not in h:
                    continue
                uid = str(h.get("SeriesInstanceUID", "")) or f"nouid-{h.get('SeriesNumber', 0)}"
                groups[uid].append(path)
                heads.setdefault(uid, h)
            verdicts = {uid: classify_series(heads[uid], len(files)) for uid, files in groups.items()}
            keep_uids = {u for u, (v, _) in verdicts.items() if v == "keep"}
            pick_note = ""
            if study_id in picks:
                # the analysed series wins: match on UID first, then on description (copies re-anonymised elsewhere get new UIDs)
                chosen = set()
                for pk in picks[study_id]:
                    if pk["uid"] and pk["uid"] in groups:
                        chosen.add(pk["uid"]); pick_stats["uid"] += 1
                    elif pk["desc"]:
                        cands = [u for u, h in heads.items() if norm_desc(h.get("SeriesDescription", "")) == norm_desc(pk["desc"])]
                        if cands:
                            chosen.update(cands); pick_stats["desc"] += 1
                        else:
                            pick_stats["unmatched"] += 1
                    else:
                        pick_stats["unmatched"] += 1
                if chosen:
                    thin_chosen = {u for u in chosen if (series_info(heads[u])[0] or 0) <= MAX_SLICE_MM}
                    if thin_chosen:
                        keep_uids = thin_chosen
                        pick_note = "analysed series (FAI) selected"
                        for u in thin_chosen:
                            verdicts[u] = ("keep", "analysed series (FAI pick)")
                    else:
                        # the analysed series is a thick recon (e.g. 1 mm lung, 2 mm); the reader needs <= 0.75 mm,
                        # so fall back to the rule-based thin coronary series and flag it
                        th_txt = ", ".join(f"{series_info(heads[u])[0]:g} mm" for u in chosen)
                        pick_note = f"FAI pick is {th_txt} - thin recon used instead - CHECK"
                        check_studies.append(study_id)
                        for u in chosen:
                            verdicts[u] = ("drop", verdicts[u][1] + f" [FAI pick but {series_info(heads[u])[0]:g} mm]")
                else:
                    unmatched_picks.append(study_id)
                    pick_note = "FAI pick NOT found - rule fallback"
            if not keep_uids:  # nothing certain: fall back to thin cardiac-sized recons lacking contrast/phase info
                keep_uids = {u for u, (v, _) in verdicts.items() if v == "maybe"}
                if keep_uids:
                    check_studies.append(study_id)
            if not keep_uids:
                # last resort: an export that is a single original thin axial CT series with >=100 images is
                # almost certainly the CTCA whatever it is called; keep it and flag it for checking
                last = []
                for u, h in heads.items():
                    it = "\\".join(str(x) for x in (h.get("ImageType", []) or [])).upper()
                    th = series_info(h)[0]
                    # GE SmartPhase / ASIR exports label the coronary recon DERIVED\PRIMARY; accept PRIMARY, refuse SECONDARY / localisers
                    if str(h.get("Modality", "")) == "CT" and "PRIMARY" in it and "LOCALIZER" not in it and "SECONDARY" not in it \
                            and th is not None and th <= MAX_SLICE_MM and (len(groups[u]) >= 100 or int(h.get("NumberOfFrames", 0) or 0) >= 100):
                        last.append(u)
                if len(last) == 1:
                    keep_uids = set(last)
                    verdicts[last[0]] = ("maybe", verdicts[last[0]][1] + " [only thin primary series in export]")
                    check_studies.append(study_id)
            for uid, files in groups.items():
                v, why = verdicts[uid]
                dec = "keep" if uid in keep_uids else "drop"
                if v == "maybe" and dec == "keep":
                    why += " [kept as fallback - CHECK]"
                series_rows.append([study_id, str(heads[uid].get("SeriesNumber", "")), str(heads[uid].get("SeriesDescription", ""))[:60],
                                    len(files), dec, why])
            if not a.dry_run:
                # append this study's decisions straight away so a crash or unplugged drive loses nothing
                try:
                    new_log = not series_log.exists()
                    with open(series_log, "a", newline="") as fh:
                        w = csv.writer(fh)
                        if new_log:
                            w.writerow(["study_id", "series_number", "original_description", "images", "decision", "reason"])
                        w.writerows(r for r in series_rows if r[0] == study_id)
                except OSError:
                    pass
            if not keep_uids:
                log(f"[{n}/{len(pairs)}] {study_id} <- {folder.name}: ** NO CORONARY SERIES FOUND ({len(groups)} series) - skipped **")
                summary.append((study_id, str(folder), dict(rec, files=0)))
                no_ctca.append(study_id)
                continue
        write_failed = None
        for path in iter_dicom_files(folder, None):
            wd.touch(path)
            try:
                ds = pydicom.dcmread(str(path), force=True)
            except Exception:
                rec["nondicom"] += 1
                continue
            if "SOPClassUID" not in ds:
                rec["nondicom"] += 1
                continue
            if keep_uids is not None:
                uid = str(ds.get("SeriesInstanceUID", "")) or f"nouid-{ds.get('SeriesNumber', 0)}"
                if uid not in keep_uids:
                    rec["dropped"] += 1
                    continue
            rec["orig_patient_ids"].add(str(ds.get("PatientID", "")))
            rec["orig_names"].add(str(ds.get("PatientName", "")))
            rec["orig_study_dates"].add(str(ds.get("StudyDate", "")))
            rec["orig_sex"].add(str(ds.get("PatientSex", "")))
            rec["orig_dob"].add(str(ds.get("PatientBirthDate", "")))
            rec["orig_series_desc"].add(f"{ds.get('SeriesNumber', '')}: {ds.get('SeriesDescription', '')}")
            rec["orig_manufacturer"].add(f"{ds.get('Manufacturer', '')} {ds.get('ManufacturerModelName', '')}".strip())
            reason = is_review_object(ds)
            series_no = str(ds.get("SeriesNumber", "0") or "0")
            inst_no = str(ds.get("InstanceNumber", "") or "")
            try:
                anonymise_dataset(ds, study_id, salt, a.keep_technical)
            except Exception as e:
                rec["errors"] += 1
                file_rows.append([study_id, str(path), "", "ERROR", str(e)])
                continue
            try:
                sno = int(float(series_no))
            except ValueError:
                sno = 0
            if reason:
                dest_dir = out / "_review" / safe_name(study_id)
                rec["review"] += 1
            elif a.flat:
                dest_dir = out / safe_name(study_id)
            else:
                dest_dir = out / safe_name(study_id) / f"S{sno:03d}"
            counter += 1
            try:
                fname = f"{safe_name(study_id)}_S{sno:03d}_I{int(float(inst_no)):05d}.dcm" if inst_no else f"{safe_name(study_id)}_{counter:06d}.dcm"
            except ValueError:
                fname = f"{safe_name(study_id)}_{counter:06d}.dcm"
            dest = dest_dir / fname
            if dest.exists():
                dest = dest_dir / f"{dest.stem}_{counter}.dcm"
            if not a.dry_run:
                try:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    ds.save_as(str(dest), write_like_original=False)
                except OSError as e:
                    write_failed = e
                    break
            rec["files"] += 1
            rec["series"].add(series_no)
            file_rows.append([study_id, str(path), str(dest), "REVIEW" if reason else "OK", reason or ""])
        study_skips = [x for x in SKIPPED_HITS if x.startswith(str(folder))]
        for x in study_skips:
            file_rows.append([study_id, x, "", "SKIPPED", "listed in skip_files.txt"])
        if write_failed is not None:
            if not volume_present(out):
                log(f"\n** Output drive disappeared while writing {study_id} ({write_failed}). Stopping at study {n}. "
                    f"Reconnect the drive and re-run with --resume; this study will be redone. **")
                drive_lost = True
                break
            rec["errors"] += 1
            file_rows.append([study_id, "", str(study_out), "ERROR", f"write failed: {write_failed}"])
        done_n = n - skipped
        elapsed = time.time() - t0
        eta = (elapsed / done_n) * (len(pairs) - n) / 60 if done_n else 0
        log(f"[{n}/{len(pairs)}] {study_id} <- {folder.name}: {rec['files']} files, {len(rec['series'])} series"
            + f"  ({n * 100 // len(pairs)}%, ~{eta:.0f} min left)"
            + (f", {rec['review']} to _review" if rec["review"] else "") + (f", {rec['errors']} ERRORS" if rec["errors"] else "")
            + (f", {rec['dropped']} non-CTCA images dropped" if rec["dropped"] else "")
            + (f"  [{pick_note}]" if a.ctca_only and pick_note else "")
            + ("  ** NO DICOM FILES FOUND **" if rec["files"] == 0 else "")
            + (f"  [{len(study_skips)} listed file(s) skipped - CHECK]" if study_skips else ""))
        if not a.dry_run and rec["files"] and not rec["errors"]:
            try:
                study_out.mkdir(parents=True, exist_ok=True)
                marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
            except OSError as e:
                log(f"\n** Could not write the completion marker for {study_id} ({e}); drive gone? Stopping. **")
                drive_lost = True
                break
        summary.append((study_id, str(folder), rec))
        link_rows.append([study_id, str(folder), ";".join(sorted(rec["orig_patient_ids"])), ";".join(sorted(rec["orig_names"])),
                          ";".join(sorted(rec["orig_study_dates"])), ";".join(sorted(rec["orig_sex"])), ";".join(sorted(rec["orig_dob"])),
                          ";".join(sorted(rec["orig_manufacturer"])), " | ".join(sorted(rec["orig_series_desc"])),
                          rec["files"], len(rec["series"]), rec["review"], rec["errors"]])
    wd.stop()
    if a.dry_run:
        log(f"\nDRY RUN: {len(summary)} studies would be written; {len(missing)} folders not found")
        return 0
    logs = out / "_logs"
    link_path = Path(a.linkage_log) if a.linkage_log else logs / f"LINKAGE_{run_ts}_CONFIDENTIAL.csv"
    if drive_lost or not volume_present(out):
        log(f"** Drive gone: the per-run CSV logs for this attempt could not be written. Completed studies are marked on disk; "
            f"re-run with --resume when the drive is back. **")
        return 1
    try:
        with open(logs / f"files_{run_ts}.csv", "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["study_id", "source_path", "output_path", "status", "note"]); w.writerows(file_rows)
        with open(link_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["study_id", "source_folder", "original_patient_id", "original_name", "original_study_date", "original_sex",
                        "original_dob", "original_manufacturer", "original_series_descriptions", "files", "series", "review_files", "errors"])
            w.writerows(link_rows)
        if series_rows and not series_log.exists():
            with open(series_log, "w", newline="") as fh:
                w = csv.writer(fh); w.writerow(["study_id", "series_number", "original_description", "images", "decision", "reason"]); w.writerows(series_rows)
        with open(logs / f"summary_{run_ts}.csv", "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["study_id", "source_folder", "files", "series", "review_files", "errors", "status"])
            for sid, src, r in summary:
                w.writerow([sid, src, r["files"], len(r["series"]), r["review"], r["errors"],
                            "NO CTCA SERIES" if sid in no_ctca else ("EMPTY" if r["files"] == 0 else ("ERRORS" if r["errors"] else "OK"))])
            for src, sid in missing:
                w.writerow([sid, src, 0, 0, 0, 0, "FOLDER NOT FOUND"])
    except OSError as e:
        log(f"** Could not write the run logs ({e}). Completed studies are marked on disk; re-run with --resume. **")
        return 1
    total = sum(r["files"] for _, _, r in summary)
    empty = [sid for sid, _, r in summary if r["files"] == 0]
    log(f"\nDone in {(time.time() - t0) / 60:.1f} min: {len(summary)} studies, {total} files written to {out}")
    if no_ctca:
        log(f"  Studies with NO coronary CTA series ({len(no_ctca)}) - need re-export: {', '.join(no_ctca)}")
    if check_studies:
        log(f"  Studies kept on fallback rule (no contrast/phase in header) - CHECK ({len(check_studies)}): {', '.join(check_studies)}")
    if SKIPPED_HITS:
        log(f"  Source files skipped via skip_files.txt ({len(SKIPPED_HITS)}) - those studies are missing a slice, CHECK: "
            + ", ".join(SKIPPED_HITS))
    if picks:
        log(f"  Analysed-series picks: {pick_stats['uid']} matched by UID, {pick_stats['desc']} by description, "
            f"{len(unmatched_picks)} studies not matched (rule used - CHECK): {', '.join(unmatched_picks)}")
    empty = [e for e in empty if e not in no_ctca]
    if empty:
        log(f"  Studies with NO DICOM files found ({len(empty)}): {', '.join(empty)}")
    if missing:
        log(f"  Folders not found ({len(missing)}): {', '.join(s for _, s in missing)}")
    log(f"  Per-study summary: {logs / f'summary_{run_ts}.csv'}")
    log(f"  Linkage log (confidential, move it out of the output tree): {link_path}")
    return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

PN_KEYWORDS = ("PatientName", "ReferringPhysicianName", "PhysiciansOfRecord", "PerformingPhysicianName",
               "OperatorsName", "RequestingPhysician", "NameOfPhysiciansReadingStudy")


def cmd_verify(a: argparse.Namespace) -> int:
    out = Path(a.output).resolve()
    needles: set[str] = set()
    if a.mapping:
        needles |= {k for k in load_mapping(a.mapping, a.current_col, a.new_col, a.sheet).keys() if len(k) >= 4}
    for n in a.needle or []:
        needles.add(n.upper())
    # Read the linkage log(s) too: original IDs/names in there are exactly what must not appear in scans.
    for lp in (out / "_logs").glob("LINKAGE_*.csv"):
        with open(lp, newline="") as fh:
            for r in csv.DictReader(fh):
                for col in ("original_patient_id", "original_name", "original_study_date", "original_dob"):
                    for v in (r.get(col) or "").split(";"):
                        v = v.strip().upper()
                        if len(v) >= 4 and v not in ("19000101", "00010101"):
                            needles.add(v)

    problems = defaultdict(list)
    checked = 0
    for path in iter_dicom_files(out, None):
        if "_review" in path.parts:
            continue
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        except Exception:
            continue
        if "SOPClassUID" not in ds:
            continue
        checked += 1
        rel = str(path.relative_to(out))

        def flag(kind, detail):
            problems[kind].append(f"{rel}: {detail}")

        def walk(d: Dataset, depth=0):
            for e in d:
                if e.tag.is_private:
                    flag("private tag present", str(e.tag))
                elif e.VR == "SQ":
                    for it in e.value:
                        walk(it, depth + 1)
                elif e.VR == "DA" and e.value and e.value != DUMMY_DATE:
                    flag("real date", f"{e.keyword}={e.value}")
                elif e.VR == "DT" and e.value and not str(e.value).startswith(DUMMY_DATE):
                    flag("real datetime", f"{e.keyword}={e.value}")
                elif e.VR == "TM" and e.value and str(e.value) != DUMMY_TIME:
                    flag("real time", f"{e.keyword}={e.value}")
                elif e.VR == "UI" and e.keyword not in UID_KEEP and e.value and not str(e.value).startswith(("2.25.", STANDARD_UID_PREFIX)):
                    flag("original UID", f"{e.keyword}={e.value}")
                elif e.VR in ("PN", "LO", "SH", "ST", "LT", "UT", "CS", "AE") and e.value:
                    txt = str(e.value).upper()
                    for n in needles:
                        if n in txt and e.keyword not in ("PatientName", "PatientID"):
                            flag("identifier text", f"{e.keyword} contains {n}")
                        elif n in txt and txt != n:
                            flag("identifier text", f"{e.keyword}={txt}")

        walk(ds)
        for kw in ("PatientBirthDate", "PatientSex", "PatientAge", "OtherPatientIDs", "DeviceSerialNumber", "SoftwareVersions"):
            if kw in ds:
                flag("attribute should be absent", kw)
        for kw in ("InstitutionName", "Manufacturer", "ManufacturerModelName", "StationName", "SeriesDescription",
                   "StudyDescription", "ProtocolName", "AccessionNumber"):
            if kw in ds and str(ds[kw].value):
                flag("attribute should be empty", f"{kw}={ds[kw].value}")
        for kw in ("ConvolutionKernel", "ScanOptions", "FilterType"):
            if kw in ds and str(ds[kw].value) and not a.keep_technical:
                flag("vendor hint present", f"{kw}={ds[kw].value}")
        for kw in PN_KEYWORDS:
            if kw in ds and kw != "PatientName" and str(ds[kw].value):
                flag("person name present", f"{kw}={ds[kw].value}")
        if str(ds.get("PatientName", "")) != str(ds.get("PatientID", "")):
            flag("PatientName != PatientID", f"{ds.get('PatientName')} / {ds.get('PatientID')}")
        if str(ds.get("PatientIdentityRemoved", "")) != "YES":
            flag("PatientIdentityRemoved not YES", "")

    report = out / "_logs" / f"verify_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    lines = [f"Verified {checked} files under {out}", ""]
    if not problems:
        lines.append("PASS: no residual identifiers, real dates, original UIDs or private tags found.")
    else:
        lines.append(f"FAIL: {sum(len(v) for v in problems.values())} findings")
        for kind, items in sorted(problems.items()):
            lines.append(f"\n[{kind}] x{len(items)}")
            lines.extend("  " + x for x in items[:a.max_examples])
            if len(items) > a.max_examples:
                lines.append(f"  ... and {len(items) - a.max_examples} more")
    report.parent.mkdir(exist_ok=True)
    report.write_text("\n".join(lines))
    log("\n".join(lines))
    log(f"\nReport written to {report}")
    return 1 if problems else 0


# ---------------------------------------------------------------------------

def cmd_thick(a: argparse.Namespace) -> int:
    """Audit an output tree: list studies whose kept series are thicker than MAX_SLICE_MM; with --fix remove their
    completion markers so the next --resume run redoes them under the current rule."""
    out = Path(a.output)
    bad = []
    for study in sorted(p for p in out.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        thick_series = []
        for sdir in sorted(p for p in study.iterdir() if p.is_dir()):
            f = next((x for x in sdir.iterdir() if x.is_file() and not x.name.startswith(".")), None)
            if not f:
                continue
            try:
                h = pydicom.dcmread(str(f), stop_before_pixels=True, specific_tags=["SliceThickness", "SharedFunctionalGroupsSequence"], force=True)
            except Exception:
                continue
            th = series_info(h)[0]
            if th is not None and th > MAX_SLICE_MM:
                thick_series.append(f"{sdir.name}={th:g} mm")
        if thick_series:
            bad.append((study.name, thick_series))
    if not bad:
        log(f"All kept series are <= {MAX_SLICE_MM:g} mm.")
        return 0
    log(f"{len(bad)} studies have kept series thicker than {MAX_SLICE_MM:g} mm:")
    for sid, ts in bad:
        log(f"  {sid}: {', '.join(ts)}")
    if a.fix:
        import shutil
        for sid, _ in bad:
            shutil.rmtree(out / sid, ignore_errors=True)
            shutil.rmtree(out / "_review" / sid, ignore_errors=True)
        log(f"Removed those {len(bad)} study folders; re-run with --resume to redo them under the current rule.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="anonymise an input tree into an output tree")
    r.add_argument("--input", help="folder containing the original DICOM files (searched recursively)")
    r.add_argument("--manifest", help="CSV with columns source_folder, study_id: one patient folder per row (alternative to --input)")
    r.add_argument("--resume", action="store_true", help="manifest mode: skip studies already completed")
    r.add_argument("--remap", help="manifest mode: rewrite a path prefix, e.g. '/Volumes/Mstr_SCAD=E:\\' to run a Mac-written "
                                   "manifest from a Windows PC where the drive is E:")
    r.add_argument("--log-file", help="also append everything printed to this file")
    r.add_argument("--series-pick", help="CSV (study_id, series_uid, series_description) naming the analysed series to keep per study; "
                                          "used with --ctca-only, overrides the rule when it matches")
    r.add_argument("--ctca-only", action="store_true",
                   help="manifest mode: keep only coronary CTA series (drops localisers, calcium score, chest recons, "
                        "lung/sharp kernels, MPRs, dose reports); decisions logged to _logs/series_<ts>.csv")
    r.add_argument("--output", required=True, help="folder for anonymised copies (created if missing)")
    r.add_argument("--study-id", help="single-patient mode: the new ID to apply to everything under --input")
    r.add_argument("--mapping", help="CSV/XLSX with current ID -> new study ID columns")
    r.add_argument("--current-col", help="mapping column holding the current ID (auto-detected if omitted)")
    r.add_argument("--new-col", help="mapping column holding the new study ID (auto-detected if omitted)")
    r.add_argument("--sheet", help="worksheet name for .xlsx mappings (default: the active sheet)")
    r.add_argument("--match-on", choices=("patientid", "folder"), default="patientid",
                   help="what to look up in the mapping: the DICOM PatientID, or the top-level subfolder name under --input")
    r.add_argument("--keep-technical", action="store_true",
                   help="keep vendor-specific technical fields (convolution kernel, scan options, filter type, full ImageType)")
    r.add_argument("--flat", action="store_true", help="one folder per study with no per-series subfolders")
    r.add_argument("--linkage-log", help="where to write the confidential linkage CSV (default: <output>/_logs/)")
    r.add_argument("--salt", help="fixed UID salt (default: random, saved in <output>/_logs/uid_salt.txt for re-runs)")
    r.add_argument("--dry-run", action="store_true", help="scan and report, write nothing")
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("verify", help="scan an anonymised output tree for residual identifiers")
    v.add_argument("--output", required=True)
    v.add_argument("--mapping", help="mapping file; its current IDs become search strings")
    v.add_argument("--current-col")
    v.add_argument("--new-col")
    v.add_argument("--sheet")
    v.add_argument("--needle", action="append", help="extra string that must not appear (repeatable), e.g. a surname")
    v.add_argument("--keep-technical", action="store_true", help="do not flag vendor-specific technical fields")
    v.add_argument("--max-examples", type=int, default=10)
    v.add_argument("--log-file", help="also append everything printed to this file")
    v.set_defaults(func=cmd_verify)

    t = sub.add_parser("thick", help=f"list (and with --fix remove, for redo) output studies whose kept series exceed {MAX_SLICE_MM:g} mm")
    t.add_argument("--output", required=True)
    t.add_argument("--fix", action="store_true")
    t.add_argument("--log-file")
    t.set_defaults(func=cmd_thick)

    a = p.parse_args(argv)
    if getattr(a, "log_file", None):
        global LOG_FILE
        LOG_FILE = a.log_file
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
