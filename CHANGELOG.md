# Changelog

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
- Tests 51 -> 61.

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
