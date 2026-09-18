# Scrub-DICOM: working notes for Claude Code

## What this is
Batch pseudonymisation of cardiac CT (CTCA) DICOM for blinded research reads. Built by and credited to Dr Charles Badu-Boateng; copyright and licence holder BB & Co Holdings Ltd,
PolyForm Noncommercial. Maintainer: Dr Charles Badu-Boateng (cardiology registrar, SCAD PhD). The v0.1 engine
in `scrubdicom/core.py` has been validated on ~520 real studies (Siemens, GE, Canon, Philips exports) and is the
thing not to break. Read `README.md`, `CHANGELOG.md` and `docs/tag_policy.md` before changing behaviour.

## Non-negotiables
- Originals are never modified. Output goes to a separate tree with one folder per study ID.
- Every output file must pass `scrub-dicom verify`. Any change to the tag policy needs a fixture and a test.
- Nothing in the output may identify patient, centre or scanner (blinded multi-diagnosis reader study).
- Dates are dummied (`19000101` / `111111.111111`), never shifted; UIDs are regenerated deterministically from a salt.
- `--ctca-only` keeps series <= `MAX_SLICE_MM` (0.8, i.e. 0.75 mm and thinner). Do not raise it without asking.
- The linkage log, the UID salt and the run logs belong in the confidential folder (`--confidential`), never in the
  output tree; the app requires one. Verify writes a checksum manifest + attestation on PASS; `--recheck` guards hand-over.
- Profiles may only *retain* PS3.15 retain-option attributes (see `scrubdicom/profiles.py`). Never add an option
  that keeps a private tag, a UID, a name, an accession number, a comment or an address. The default profile must
  stay identical to the validated policy (`test_default_profile_matches_no_profile`).
- Runs are long (hours) on external drives that fail: keep the resume markers, drive-loss stop, watchdog,
  `skip_files.txt`, built-in keep-awake, and Windows path handling (`--remap`, long paths, trailing-space folders).
- Must work on macOS and Windows without extra steps beyond Python. Test both code paths when touching paths.

## Layout
- `scrubdicom/core.py`     engine + CLI (`run`, `verify`, `thick`)
- `scrubdicom/profiles.py` de-identification profiles: the only knobs a user may turn (PS3.15 retain options +
                           replacement texts). The floor cannot be switched off. Every option has a test.
- `scrubdicom/ocr.py`      optional Tesseract OCR of quarantined objects (nothing bundled)
- `scrubdicom/survey.py`   standalone series survey / select
- `scrubdicom/fixtures.py` synthetic test patients with planted identifiers (no real data anywhere in the repo)
- `scrubdicom/app/`       desktop app: `model.py` logic (no Tk, unit-tested), `runner.py` engine child process,
                           `ui.py` the Tk window, `preview.py` viewer logic (scan, header diff, pixels, redaction,
                           reformats; no Tk), `viewer.py` the viewer window, `profile_ui.py` profile editor,
                           `theme.py` Sun Valley light/dark theme, `__init__.py` entry with `--cli` and `--selftest`
- `packaging/`             PyInstaller spec, macOS/Windows build scripts, Inno Setup script, icons
- `launchers/`             double-click RUN_ME.command / RUN_ME.bat templates
- `tests/`                 pytest; run `pytest` before every commit
- `docs/`                  tag policy, packaging evaluation, security notes, app user guide

## v0.2 (done): desktop app
Native Tk window over the unchanged engine, packaged with PyInstaller (decision record: `docs/packaging_evaluation.md`).
The app never imports the engine into its own process: it spawns `<itself> --cli run ...` (frozen) or
`python -m scrubdicom run ...` (source) and streams the output, so Stop is a real kill and every run is reproducible
from a terminal. Rules for app work:
- Logic goes in `scrubdicom/app/model.py` with a test; `ui.py` only draws and delegates.
- The app must never import a network module (`tests/test_repo_hygiene.py` enforces it) and must never persist an
  identifier in settings (`SETTINGS_KEYS` allow-list).
- Treat the whole `_logs` folder as confidential, not just LINKAGE. See `docs/security.md`.
- Build with `packaging/build_macos.sh` / `build_windows.bat` from python.org Python, never conda. A build that
  fails the smoke tests ships nothing.
- Before a release: run the frozen app on a real export from each vendor and verify it. Then sign and notarise.

## Conventions
- British English in docs and UI. Plain, direct language; no marketing tone.
- Never commit `.dcm`, `_logs/`, `_review/` or anything named `*_CONFIDENTIAL.csv` (see `.gitignore`).
- Commit messages: short imperative summary, then why.
