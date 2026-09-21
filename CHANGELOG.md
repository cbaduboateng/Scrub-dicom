# Changelog

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
