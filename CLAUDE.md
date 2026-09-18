# Scrub-DICOM: working notes for Claude Code

## What this is
Batch pseudonymisation of cardiac CT (CTCA) DICOM for blinded research reads. Owned by BB & Co Consulting Ltd,
PolyForm Noncommercial licence. Maintainer: Charles Badu-Boateng (cardiology registrar, SCAD PhD). The v0.1 engine
in `scrubdicom/core.py` has been validated on ~520 real studies (Siemens, GE, Canon, Philips exports) and is the
thing not to break. Read `README.md`, `CHANGELOG.md` and `docs/tag_policy.md` before changing behaviour.

## Non-negotiables
- Originals are never modified. Output goes to a separate tree with one folder per study ID.
- Every output file must pass `scrub-dicom verify`. Any change to the tag policy needs a fixture and a test.
- Nothing in the output may identify patient, centre or scanner (blinded multi-diagnosis reader study).
- Dates are dummied (`19000101` / `111111.111111`), never shifted; UIDs are regenerated deterministically from a salt.
- `--ctca-only` keeps series <= `MAX_SLICE_MM` (0.8, i.e. 0.75 mm and thinner). Do not raise it without asking.
- Runs are long (hours) on external drives that fail: keep the resume markers, drive-loss stop, watchdog,
  `skip_files.txt`, built-in keep-awake, and Windows path handling (`--remap`, long paths, trailing-space folders).
- Must work on macOS and Windows without extra steps beyond Python. Test both code paths when touching paths.

## Layout
- `scrubdicom/core.py`     engine + CLI (`run`, `verify`, `thick`)
- `scrubdicom/survey.py`   standalone series survey / select
- `scrubdicom/fixtures.py` synthetic test patients with planted identifiers (no real data anywhere in the repo)
- `scrubdicom/dashboard/`  Streamlit dashboard (v0.1: developer-grade)
- `launchers/`             double-click RUN_ME.command / RUN_ME.bat templates
- `tests/`                 pytest; run `pytest` before every commit

## v0.2 goal
A packaged desktop app (no Python install for the user) with the dashboard as the front end and the unchanged
engine behind it: pick input/manifest/output, choose options, watch progress, review series decisions (kept /
dropped / needs a look), read verify, export logs. Candidates: PyInstaller or Briefcase; evaluate and propose
before building. Keep the CLI working; the app wraps it, it does not replace it.

## Conventions
- British English in docs and UI. Plain, direct language; no marketing tone.
- Never commit `.dcm`, `_logs/`, `_review/` or anything named `*_CONFIDENTIAL.csv` (see `.gitignore`).
- Commit messages: short imperative summary, then why.
