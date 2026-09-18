# Scrub-DICOM

Batch pseudonymisation of cardiac CT (CTCA) DICOM studies for blinded research reads.

Scrub-DICOM takes a folder (or many folders) of PACS exports and produces a separate, anonymised copy in
which nothing in the header identifies the **patient**, the **centre** or the **scanner**, so scans from
different hospitals and different diagnoses can be read blind. It replaces the manual TeraRecon /
DICOM Anonymizer Pro workflow with a scripted, resumable, self-verifying batch job. Originals are never
modified.

It was built for a multi-centre SCAD reader study (hundreds of studies, five vendors, several export
tools) and hardened against the things that actually went wrong there: private tags carrying the referring
clinician and the true study date, vendor kernel names giving the centre away, a failing external drive,
exports with a dozen irrelevant series, and a Windows PC that could not see a folder with a trailing space.

## What it does to every file

| Category | Action |
|---|---|
| PatientName, PatientID | set to the new study ID |
| DOB, sex, age, addresses, other IDs, issuer | removed |
| Every date / time / datetime, in every sequence | dummied to `19000101` / `111111.111111` |
| Every UID except standard `1.2.840.10008.*` | replaced by a deterministic SHA-256 `2.25.*` UID (same salt = same study stays one study) |
| All private tags | removed (this is where clinician names, true datetimes and age-in-days were found) |
| Institution, station, manufacturer, model, serial, software, physicians, operators, accession, order numbers, procedure codes, descriptions, protocol | cleared or removed |
| Kernel, scan options, filter, ImageType beyond the third value | removed unless `--keep-technical` |
| File meta | rewritten: new MediaStorage UID, sending/receiving AE titles dropped, transfer syntax preserved (compressed pixels copied byte-for-byte) |
| Secondary captures, SRs, presentation states, PDFs, burned-in annotation | header-anonymised but written to `_review/` for a human to look at |

Full attribute list and rationale: [docs/tag_policy.md](docs/tag_policy.md).

`--ctca-only` keeps just the coronary reconstruction: original/primary CT, slice thickness <= 0.75 mm,
>= 100 images, reconstruction FOV <= 260 mm, soft kernel, and contrast / cardiac phase / CARDIAC_CTA in the
header. Vendor naming quirks (GE "SnapShot Pulse", Siemens "Thoracic Aorta" gated cardiac runs, GE
SmartPhase recons labelled DERIVED) are handled. Every decision is written to `_logs/series_<ts>.csv`.

`verify` re-opens every output file and fails on any private tag, real date, original UID, vendor or
institution text, person name, or any string you pass with `--needle`.

## Install

```
pip install scrub-dicom            # when published; until then:
pip install git+https://github.com/cbaduboateng/Scrub-dicom
```

Python 3.9+, macOS / Windows / Linux. `pip install "scrub-dicom[xlsx]"` for Excel mapping files,
`"scrub-dicom[app]"` for the desktop app from source.

## Quick start

One patient, explicit ID:

```
scrub-dicom run --input "/scans/P017" --output "/anon" --study-id STUDY-017
```

A cohort in one folder with a spreadsheet mapping current IDs to study IDs:

```
scrub-dicom run --input "/scans/Part 1" --output "/anon" \
    --mapping ids.xlsx --current-col "Hospital ID" --new-col "Study ID" --match-on folder
```

A cohort scattered across several folders, coronary series only, resumable:

```
scrub-dicom run --manifest cohort.csv --output "/anon" --resume --ctca-only
scrub-dicom verify --output "/anon" --needle <consultant surname>
```

`cohort.csv` has two columns, `source_folder,study_id`, one patient folder per row
([example](examples/manifest_example.csv)). Add `--series-pick analysed.csv` (`study_id,series_uid,
series_description`) when a particular series has already been analysed and must be the one kept; if that
series is thicker than 0.75 mm the thin coronary recon is used instead and the study is flagged.

The `launchers/` folder has a double-clickable `RUN_ME.command` (macOS) and `RUN_ME.bat` (Windows) that
install the package if needed, audit finished studies, run, and verify.

### Desktop app (no Python needed)

Download the `.dmg` (macOS) or the setup `.exe` / zip (Windows) from the release, or build it yourself with
`packaging/build_macos.sh` / `packaging\build_windows.bat` (see [packaging/README.md](packaging/README.md)).
The app is a plain window that wraps the same engine: choose a manifest or folders, set options, dry-run,
start, watch progress, stop; review series decisions; run verify with your own needles; and a "Ready to share?"
check that moves the confidential logs out of the output tree. A built-in DICOM viewer previews exactly what a run
will do to each patient (series decisions, images, header before/after) and, afterwards, lets you redact burned-in
text in quarantined files and release them. De-identification profiles let a study keep, for example, sex and a
5-year age bucket or shifted dates, within the limits DICOM PS3.15 allows; the default removes everything. It never
connects to the internet. User guide:
[docs/app_user_guide.md](docs/app_user_guide.md). From source: `pip install -e .` then `scrub-dicom-app`.

## Built for long unattended runs on unreliable hardware

The computer is kept awake for the duration (macOS `caffeinate`, Windows power request), no launcher
tricks needed. A study is marked complete only when fully written; `--resume` skips complete studies and
redoes half-finished ones. If the output drive disappears the run stops cleanly instead of racing through
the manifest, and a watchdog names the exact file when a failing drive hangs on a read. Files listed in a
`skip_files.txt` next to the manifest are never opened. Manifests written on a Mac run unchanged on Windows
with `--remap "/Volumes/Drive=E:\"`, including folders with trailing spaces. `thick --fix` audits an
existing output tree against the current slice-thickness rule and clears anything that no longer qualifies
so it is redone.

## Output layout

```
<out>/
  STUDY-017/S004/STUDY-017_S004_I00001.dcm ...    one folder per study, one per kept series
  _review/STUDY-017/...                          quarantined objects, check by eye
  _logs/LINKAGE_<ts>_CONFIDENTIAL.csv            study ID -> original ID/name/DOB/date/scanner. MOVE THIS OUT before sharing.
  _logs/series_<ts>.csv                          every series decision with reason
  _logs/summary_<ts>.csv, files_<ts>.csv         per-study and per-file record
  _logs/verify_<ts>.txt                          verify report
  _logs/uid_salt.txt                             keep with the output if re-runs must produce the same UIDs
```

## Tests

```
pip install "scrub-dicom[test]"
pytest
```

`scrubdicom.fixtures` builds two synthetic patients with identifiers planted in private tags, sequences,
comments and contrast times, plus a dose SR; the tests run, verify and inspect the result. The app tests drive the
same engine through the app's child-process runner, and the repo-hygiene tests fail if a scan, linkage file, secret
or network import is ever committed.

## Security

Where identifiable data lives while the tool runs, what the software will never do, and what remains the
operator's job: [docs/security.md](docs/security.md). Short version: the whole `_logs` folder is confidential,
verify must pass, `_review` needs a human, and the app never touches the network.

## Status and roadmap

v0.1: command line + minimal dashboard, validated on ~520 real studies from Siemens, GE, Canon and
Philips exports. v0.2: packaged desktop app (macOS / Windows, no Python install) wrapping the unchanged engine.
Planned: per-series thumbnails, pixel-level burned-in text detection for secondary captures, configurable tag
policy profiles, signed and notarised releases.

## Credits and licence

Built by Dr Charles Badu-Boateng. Copyright BB & Co Holdings Ltd. PolyForm Noncommercial 1.0.0: free for research, education and personal use;
commercial use requires a licence from BB & Co Holdings Ltd (charles@bbandcoconsulting.com). See [LICENSE](LICENSE). Scrub-DICOM is a tool, not a legal opinion: you remain
responsible for confirming that your output meets your institution's and regulator's requirements.
