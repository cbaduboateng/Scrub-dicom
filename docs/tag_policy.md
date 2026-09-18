# Tag policy and rationale

The policy follows DICOM PS3.15 Annex E "Basic Application Level Confidentiality Profile" with the
Retain Safe Private option **not** applied (all private tags go), the Clean Descriptors option applied
(descriptions blanked), and the Retain Longitudinal Temporal Information option **not** applied (dates
dummied rather than shifted). On top of that it removes equipment and vendor vocabulary, which PS3.15
permits to keep, because the use case is a blinded multi-diagnosis reader study.

## Evidence from real exports

Two previously "anonymised" files from the same cohort, produced by different tools, were inspected:

| Leak | Where | Tool that missed it |
|---|---|---|
| Referring clinician name + GMC-style number | ELSCINT1 private (07A3,1019)/(07A3,101C) | OsiriX-based |
| True study datetime `20171107154316` | ELSCINT1 private (07A1,105D), (07A5,1054) | OsiriX-based |
| Age in days (`20403`, i.e. DOB to within a day) | ELSCINT1 private (07A3,1034) "Tamar Study Age" | OsiriX-based |
| Real contrast injection clock time | ContrastBolusStartTime (0018,1042) | OsiriX-based |
| Sending PACS AE title `(hospital PACS gateway AE title)` | file meta (0002,0017) | OsiriX-based |
| ODS code `RWE` | IssuerOfPatientID (0010,0021) | OsiriX-based |
| Original Siemens Frame of Reference / Irradiation Event UIDs | (0020,0052), (0008,3010) | OsiriX-based |
| Filler order number | (0040,2017) | OsiriX-based |
| Top-level PatientID absent (only in OtherPatientIDsSequence) | (0010,0020) | OsiriX-based |
| True StudyDate `20181023` | (0008,0020) | DCMTK-based |
| Scanner model `syngo.via.VB30A`, `Siemens Healthineers` | (0008,1090), (0008,0070) | DCMTK-based |
| Study ID mangled `CBB0102` -> `CBB102` | (0010,0020) | DCMTK-based |
| `InstitutionName = "ANONYMIZED"` (announces the tool, harmless but untidy) | (0008,0080) | DCMTK-based |

## Categories

**Replaced with the study ID**: PatientName, PatientID.

**Removed** (element deleted): PatientBirthDate/Time, PatientSex, PatientAge, all "other" patient
IDs/names, addresses, phone, ethnic group, occupation, religion, military rank, residence, insurance,
comments (patient/study/image/identifying), issuer of patient ID, institution address/department,
device serial, software versions, gantry/plate/detector IDs, request/order/procedure sequences and
IDs, requesting physician/service, admission and clinical-trial attributes, original attributes
sequence, contributing equipment, derivation description, SR ContentSequence.

**Cleared** (kept, empty): AccessionNumber, StudyID, InstitutionName, StationName, Manufacturer,
ManufacturerModelName, ReferringPhysicianName (+ID sequence), PhysiciansOfRecord,
PerformingPhysicianName, NameOfPhysiciansReadingStudy, OperatorsName, StudyDescription,
SeriesDescription, ProtocolName, PerformedProtocolCodeSequence, PatientWeight, PatientSize.
PregnancyStatus (US) is deleted because a numeric VR cannot be empty.

**Vendor hints, removed unless `--keep-technical`**: ConvolutionKernel, ScanOptions, FilterType,
FilterMaterial, ExposureModulationType, ReconstructionAlgorithm, AcquisitionProtocolName,
ImageFilter; ImageType truncated to its first three (standard) values.

**Dates/times**: every DA -> `19000101`, TM -> `111111.111111`, DT -> `19000101111111.111111`,
recursively through sequences. Empty values stay empty.

**UIDs**: every UI element not in the standard `1.2.840.10008.` root and not in the keep-list
(SOPClassUID, TransferSyntaxUID, MediaStorageSOPClassUID, ImplementationClassUID,
ReferencedSOPClassUID, CodingSchemeUID, MappingResourceUID, ContextGroupExtensionCreatorUID) is
replaced by `2.25.` + first 128 bits of SHA-256(salt | original UID) as a decimal integer. This is
a valid UUID-derived UID, deterministic per salt, not reversible.

**Private tags**: all removed with `Dataset.remove_private_tags()` (recursive). The Siemens CSA header
is lost; it carries reconstruction internals and the vendor name, nothing the reader needs.

**File meta**: MediaStorageSOPInstanceUID = new SOPInstanceUID; Source/Sending/Receiving AE titles
and private information removed; ImplementationClassUID set to pydicom's; TransferSyntaxUID unchanged
so encapsulated (JPEG 2000 / JPEG-LS) pixel data is copied without decompression.

**Kept on purpose**: geometry (ImagePosition/Orientation, PixelSpacing, SliceThickness, SliceLocation,
FrameOfReference relationships via the hashed UID), acquisition physics (KVP, mAs, exposure, CTDIvol,
DLP, collimation, pitch), windowing/rescale, contrast agent flag and volume, BodyPartExamined,
PatientPosition, series/instance/acquisition numbers, calcium scoring mass factors, code sequences
with standard `DCM` or `SRT/SCT` designators (CTDI phantom type, derivation code). Local coding
schemes (designator `L`, `99xxx`) appear only inside procedure code sequences, which are removed.

**Quarantined to `_review/`**: SOP classes for Secondary Capture (all variants), SR (basic text,
enhanced, comprehensive, key object, X-ray radiation dose), Encapsulated PDF, Grayscale Softcopy
Presentation State; any object with Modality SR/PR/KO/DOC/OT; anything with BurnedInAnnotation=YES.
These get the full header treatment but the script makes no attempt at pixel-level redaction.
