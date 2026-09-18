# Building the desktop app

Why PyInstaller and Tk, and what was rejected: `docs/packaging_evaluation.md`.

## What is here

| File | Purpose |
|---|---|
| `scrub_dicom.spec` | PyInstaller spec: entry point, data files, excluded packages, macOS bundle metadata |
| `entry.py` | the script PyInstaller freezes; it just calls `scrubdicom.app.main()` |
| `build_macos.sh` | full macOS build: venv, tests, freeze, smoke test, optional sign + notarise, `.dmg`, SHA-256 |
| `build_windows.bat` | full Windows build: venv, tests, freeze, smoke test, optional Authenticode, zip, optional Inno Setup installer |
| `windows_installer.iss` | Inno Setup script: per-user install, no admin rights |
| `entitlements.plist` | hardened-runtime entitlements for signing on macOS (no network entitlement) |
| `requirements-build.in` / `.lock` | the build's dependencies; the lock pins every wheel by SHA-256 (`pip-compile --generate-hashes`) and `pip-audit` runs on it before every build |
| `icons/make_icons.py` | draws `icon.png`, `icon.ico`, `icon.icns` |

## macOS

Needs python.org Python 3.12 (`/Library/Frameworks/Python.framework/Versions/3.12`) and Xcode
command-line tools (for `codesign`, `hdiutil`, `iconutil`). Do not build from conda; the script
refuses.

```bash
bash packaging/build_macos.sh
```

Produces `dist/Scrub-DICOM.app` and `dist/Scrub-DICOM-<version>-macOS-<arch>.dmg` plus a `.sha256`.
The app runs on the architecture of the Mac it was built on (the viewer's numpy and GDCM wheels are
single-architecture). Build on an Apple-silicon Mac for Apple-silicon users and on an Intel Mac for
Intel users; the `.dmg` name carries the architecture.

To sign and notarise, set these in the shell before running (never commit them):

```bash
export SCRUBDICOM_SIGN_IDENTITY="Developer ID Application: BB & Co Holdings Ltd (TEAMID)"
export SCRUBDICOM_NOTARY_PROFILE="scrubdicom"   # made once with: xcrun notarytool store-credentials scrubdicom
```

## Windows

Needs python.org Python 3.12 installed for the current user (the `py` launcher). Optional: Inno
Setup 6 with `iscc.exe` on PATH for the installer; `signtool` from the Windows SDK for signing.

```bat
packaging\build_windows.bat
```

Produces `dist\Scrub-DICOM\` (the runnable folder), `dist\Scrub-DICOM-<version>-windows-x64.zip`
and, if Inno Setup is present, `dist\Scrub-DICOM-<version>-setup.exe`.

To sign: `set SCRUBDICOM_SIGNTOOL_ARGS=/fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /n "BB & Co Holdings Ltd"`.

## What every build checks before it produces an artifact

1. The full pytest suite on the source tree.
2. The frozen executable answers `--cli --help`.
3. The frozen executable opens the window, draws every tab and closes (`--selftest`).
4. The frozen engine anonymises the synthetic fixture patients and its own `verify` reports PASS.
5. (macOS) The bundle contains no `requests`, `urllib3` or `tornado`, and decodes compressed pixel data.
6. Dependencies install only if every wheel's hash matches the lock; `pip-audit` finds no known vulnerability.
7. A software bill of materials (`*-sbom.json`) is written next to the DMG.

If any step fails the build stops and nothing is shipped.

## Updating a dependency

Change the pin in `requirements-build.in`, run `pip-compile --generate-hashes --allow-unsafe -o packaging/requirements-build.lock packaging/requirements-build.in`, run the build, then run the frozen app against a real
export from each vendor you support and verify it. pydicom releases occasionally change how they
write file meta or handle deprecated arguments; the engine's `save_as(..., write_like_original=False)`
call is one such place.

## Running the app from source (developers)

```bash
pip install -e ".[test,xlsx]"
scrub-dicom-app            # or: python -m scrubdicom.app
```

`python -m scrubdicom.app --selftest` is the same check the build runs.
