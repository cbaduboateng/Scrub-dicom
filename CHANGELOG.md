# Changelog

## 0.7.0 (2026-10-02)
Folder-first flow, after a live demo showed the first screen asked for lists and spreadsheets before a folder.
- **Open a folder.** The Anonymise tab is now four plain steps: Open your scans, Which scans do you want?, What
  should be removed?, Save and go. Step 1 asks one question, One patient or Several patients, and then shows one
  button. Saying "one patient" and opening a folder of several stops with a plain message and a "Switch to several
  patients" button. The app reads the headers under the folder (on a worker
  thread, with progress and a Stop button), lists the patients it found and says whether each has a coronary CT
  series. One folder per patient or per study gets an ID each; patients mixed in one folder are recognised by the
  Patient ID inside the scans; an earlier output of this app inside the folder is skipped.
- **Choose the scans.** Coronary CT only, Everything, or Let me choose: a list of the kinds of series across the
  cohort (modality, description, slice thickness, how many patients have it) with a tick each, so "only the coronary
  series, not the screening X-ray" is one click for a thousand patients. The viewer's per-patient ticks still win.
- **Choose what to remove.** Everything identifying (default), or tick what to keep (sex, 5-year age band, weight and
  height, shifted dates, scanner, technical details, hospital name) on the same screen, or a saved profile.
- **Save and go.** New IDs are numbered from a prefix, editable by double-click or filled from a spreadsheet, and
  stable across sessions: re-opening a folder with the same destination folders gives every patient the ID it had.
  The two destination folders are suggested next to the opened folder under names that never include its name.
  Anonymise no longer needs a separate Preview first in this flow: the scan and the series list are the preview.
- **One journey, one results screen.** The numbered tabs (Check series, Verify output, Share safely) are gone. The
  Anonymise tab has a fifth step, Done: one headline (tick, warning or cross), the next actions (view the scans,
  open the folder, hand over, certificate, check), and whatever still needs a person, each with its own button:
  files held back for a look, confidential logs inside the output, unfinished patients. The old tabs' content is a
  Details tab that appears on demand. A run ends on Done by itself.
- **Certificate.** A one-page PDF for every verified output (`scrubdicom/app/certificate.py`, standard library only):
  patients, files, what was removed and kept, the result, the checksum list and its SHA-256, the new IDs, what the
  check does not cover, a line to sign. No patient identifier is on it; it is written into the output's `_logs` as
  soon as the output verifies and does not disturb the checksum re-check.
- **Pictures on step 2.** Each kind of series has a thumbnail, loaded one at a time after the folder is read.
- **What was removed, in plain words.** A card shows one of the user's own files: "Name SMITH JOHN, now DEMO-001",
  "Date of birth, removed" (`scrubdicom/app/plain.py`). On step 3 it updates as options are ticked; on Done it is
  the record of what happened.
- **Less engineering on screen.** The engine's log is behind an "Activity log" toggle; the progress line estimates
  the time left; a long run ends with a desktop notification (macOS, via the system's own osascript) or the bell.
- **Find, then tick what is shown.** Step 2 has a search over the kinds of series ("cta", "pulm", "ct 1 mm"); "Tick
  shown" and "Untick shown" act on what the search found. A cohort from several hospitals names the same series
  many ways, and a cohort that is not cardiac cannot use "Coronary CT only". Series that are not coronary are now
  described for what they are ("420 images each", "scout or reformat"), not for why the coronary rule passes them over.
- **Ready for a large cohort.** The folder scan lets each top-level folder's headers go as soon as it has read that
  folder (16,000 studies would otherwise hold over a gigabyte of them), cohort totals are worked out once per change
  instead of several times per click, and the table of new IDs is rebuilt only while it is on screen. Timed on a
  simulated 16,000-study cohort: every click under a third of a second.
- **Start again.** A button at the bottom of every step (and File > Start again) clears the folder, the choices,
  the new IDs and the destinations and returns to the first question. It asks first only when IDs were typed or
  series ticked by hand. Nothing on disk is deleted or changed.
- **A confirmation you can read.** Pressing Anonymise opens a small window with a question as its heading and one
  fact per row (from, keeping, removing, where the copies go, where the key goes), with folder paths shortened and
  the full path in a tooltip. It replaces a system message box that ran five lines and two long paths together.
- **Drag and drop.** A folder dropped anywhere on the window is opened like a chosen one (tkdnd through the
  `tkinterdnd2` package, MIT, hash-pinned; optional at run time: without it the Choose button is all there is).
- The list-driven methods (patient-list CSV; folder plus ID spreadsheet) are unchanged behind "I already have a
  patient list (CSV) or an ID spreadsheet" on step 1, shown side by side. The separate "One patient" method is gone
  from the window: opening that patient's folder does the same thing. (The engine's `--input --study-id` is unchanged.)
- **Demo.** Two made-up patients with a drawn chest CT (scout, calcium score, 120-slice coronary series), a chest
  X-ray, a dose report and an echo frame, loaded into the ordinary four steps with a "Play it for me" button. The
  old demo printed a dry run of eight noise images into the log.
- **Help.** The 139-line reference is replaced on the Help tab by the four steps and nine short questions; the
  reference is one click away (Help > Full reference).
- The window's own run log now goes to the confidential folder, not the output's `_logs`, so a clean run ends
  "Verified. Safe to hand over." without a detour through "Move confidential logs out".
- New `scrubdicom/app/intake.py` (scan, kinds, IDs, the files the engine reads; no Tk; 18 tests) and
  `scrubdicom/demo_data.py`. The engine is unchanged apart from the version number.
- Found on the way: on macOS, Tk redraws for ever when a window that has never been shown is filled with data,
  which froze the app at the end of a run while Details was a separate window. It is a tab for that reason.

## 0.6.1 (2026-09-21)
First Windows build, and two Windows fixes found by CI.
- Windows: first packaged build (`Scrub-DICOM-<version>-windows-x64.zip` and a per-user `-setup.exe`), produced on
  GitHub's Windows runners by `.github/workflows/release-windows.yml` on every `v*` tag. Not code-signed: SmartScreen
  shows "unknown publisher" once; More info > Run anyway.
- Windows fix: `run --input ... --confidential` did not refuse a confidential folder inside the output tree, because the
  two paths were compared with and without the `\\?\` long-path prefix (manifest runs were unaffected). Both sides are
  now normalised before the check. Found by the first Windows CI run.
- Lock file: the Windows-only PyInstaller dependencies (`pefile`, `pywin32-ctypes`) are pinned with hashes so the
  Windows build and CI install in `--require-hashes` mode.

## 0.6.0 (2026-09-18)
Security hardening, and tick-box series selection.
- Viewer: a "Use" tick per series; "Anonymise only the ticked series" writes a selection the run honours
  (`run --select-series`), per patient, logged as "ticked / not ticked in the viewer". Live file-level progress.
- Confidential folder: `run --confidential FOLDER` writes the linkage log, the UID salt and every run log there,
  outside the output tree; the app requires one (step 2, with a Suggest button). Without it the engine warns.
  The salt now travels with the linkage material (it is what makes hashed UIDs and shifted dates linkable).
- Verify: inspects the file meta group (PACS AE titles, private information); sweeps every text field for valid
  NHS numbers (modulus 11), UK postcodes and date-shaped strings; uses surname and forename parts from the
  linkage log as whole-word needles; on PASS writes a SHA-256 manifest of every output file and a JSON
  attestation (tool, version, profile, counts, statement). `verify --recheck` compares the tree with the manifest
  and fails on any changed, missing or added file. A FAIL report goes to the confidential folder with a one-line
  stub in the output.
- App: "Hand over" re-checks the checksums first and only then opens the folder. Unhandled errors are written to
  a path-redacted error log and shown as a recovery card.
- Modalities that routinely carry burned-in text (US, XA, RF, MG, DX, CR, XC, ES, PX) are quarantined for a human
  look. Optional OCR: if Tesseract is installed, quarantined objects get a "possible burned-in text" note.
  A synthetic ultrasound frame joined the fixtures.
- Supply chain: dependencies install only by SHA-256 hash (`packaging/requirements-build.lock`), `pip-audit` runs
  before every build, a software bill of materials is written next to the DMG, and a GitHub Actions workflow
  runs the suite on macOS and Windows with `pip-audit` on every push.
- The Streamlit dashboard is removed; a test fails if any web-server module is referenced in the package.

## 0.5.0 (2026-09-18)
Idiot-proofing pass, as prepared for a conference demo.
- Guided three-step flow on the Anonymise tab (Where are the scans? / Where should the copies go? / Review and go)
  with Next disabled until each step is complete and a plain-English summary before the buttons.
- Guard rails: Anonymise is disabled until Preview has run with the same settings; hand-over is disabled until the
  check has passed and no linkage file is inside the output; destructive actions (clear thick studies, release from
  quarantine without redaction) require typing YES; an orange banner whenever the profile retains anything.
- One status strip under the header on every tab: Not started / Anonymising n of N / Verified, safe to hand over /
  Check failed, do not share.
- Empty tabs say what to do next with one button; failures show a recovery card with one action (Resume after a
  drive loss, Open report after a failed check, Go to step 1 for missing folders).
- Built-in demo: "Try it on two sample patients" runs the whole flow on synthetic scans, from Home or Help.
- The app opens clean: folder paths are no longer remembered between launches or written to the settings file.
  The frozen self-test runs the demo end to end, so a build that cannot complete the flow does not ship.
- Rarely used ways and options (ID spreadsheet mode, drive path fix, already-analysed series, technical details)
  sit behind "More ways and options".
- Home tab: Start, Try the demo, Viewer, and "where you left off".

## 0.4.0 (2026-09-18)
De-identification profiles and a refreshed interface.
- Profiles (`scrubdicom/profiles.py`): a named set of PS3.15-sanctioned options a user may retain (sex, 5-year age
  bucket, weight/height, dates shifted by a secret per-patient offset, scanner make/model, technical fields,
  institution name) and replacement values (patient name, study description, method text). The floor (private
  tags, UIDs, physicians, accession, comments, addresses) cannot be switched off. Built-in profiles: blinded read
  (default, identical to the validated policy), longitudinal follow-up, patient characteristics, scanner/technical.
  `run --profile x.json`; the profile is written to `_logs/profile.json` and into DeidentificationMethod; `verify`
  reads it and checks what the profile promised. Every option has a fixture-backed test.
- App: profile picker on the Anonymise tab and an editor (built-ins read-only; copy, edit, save your own). The
  viewer's header before/after follows the chosen profile.
- Interface: Sun Valley theme (light/dark, follows the system, toggle in the header), header bar, live validation
  that says what is missing and enables Anonymise only when the form is complete.
- Anonymise tab redesigned: three cards, tooltips instead of inline hints, an Advanced toggle for rarely used
  fields, accent and switch controls. Viewer: choose the external viewer application; open a file or a folder.
- Tests 51 -> 62.

## 0.3.0 (2026-09-18)
In-app DICOM viewer.
- Preview before committing: the "Original scans" view shows every series with its keep/drop decision and a
  thumbnail, the images, and the header before/after with each change highlighted, all computed in memory by the
  same engine code that will run. "Keep this series instead" writes a series-pick the run honours.
- Check and redact after: the "Anonymised output" view shows the written copies and the quarantined files; draw
  boxes over burned-in text and release the file into the study folder. Redactions are logged and the share
  checklist asks for a fresh verify.
- Viewing: bilinear rendering, zoom/pan, window/level presets and drag, Hounsfield readout, cine, corner
  annotations, series thumbnails, axial/coronal/sagittal reformats. Compressed pixel data via GDCM (Apache-2.0).
- "Open this file in your DICOM viewer" hands off to the installed viewer.
- Tests 38 -> 52 including a driven run of the viewer window on synthetic patients.

## 0.2.0 (2026-09-18)
Desktop app. The engine (`scrubdicom/core.py`) is unchanged apart from the version string it writes into
DeidentificationMethod.
- `scrub-dicom-app`: a native window (Tkinter, no web server, no port, no network access) that wraps the CLI:
  manifest / single-folder / mapping-file runs, dry run, live progress and log, stop, series-decision review with
  filters and CSV export, verify with needles, per-study summary, a "Ready to share?" checklist and a one-click move
  of the confidential logs out of the output tree. The exact command line is shown before every run.
- Packaged builds with PyInstaller: `packaging/build_macos.sh` (.app + .dmg, optional sign/notarise) and
  `packaging/build_windows.bat` (folder + zip, optional Inno Setup per-user installer). No Python install needed by the
  user. Every build runs the tests, then the frozen engine on synthetic patients, then verify, before producing an
  artifact. Evaluation of PyInstaller vs Briefcase and Tk vs Streamlit in `docs/packaging_evaluation.md`.
- Security: `docs/security.md` documents where identifiable data lives; the whole `_logs` folder is now treated as
  confidential (files_/summary_/unmapped_ CSVs and run logs carry original folder paths), not only the LINKAGE file.
  The developer Streamlit dashboard now binds to 127.0.0.1 only with usage telemetry off. Repo-hygiene tests fail the
  build if a scan, a linkage file, a secret, or a network import appears.
- Tests: 38 (was 9): app logic, the engine child-process runner end to end, CLI re-entry exit codes, repo hygiene.

## 0.1.0 (2026-09-18)
First public release, extracted from the SCAD reader-study pipeline.
- `run` (single folder / mapping file / manifest), `verify`, `thick`
- `--ctca-only` coronary-series selection with per-series decision log; `--series-pick` for pre-analysed series (thin-slice override)
- resumable runs with completion markers; clean stop on drive loss; read-hang watchdog; `skip_files.txt`
- built-in keep-awake (macOS caffeinate, Windows power request)
- Windows: `--remap` for Mac-written manifests, long-path and trailing-space folder handling
- Streamlit dashboard
