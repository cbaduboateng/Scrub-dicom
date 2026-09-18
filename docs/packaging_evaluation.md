# v0.2 desktop app: packaging evaluation and decision

Written before any app code, as `CLAUDE.md` asks. Two decisions had to be made: what the app's
front end is built with, and what turns it into a double-clickable program that needs no Python
install. They are linked, because the second constrains the first.

## What the app has to do

From `CLAUDE.md`: pick input / manifest / output, choose options, watch progress, review series
decisions (kept / dropped / needs a look), read verify, export logs. The engine in
`scrubdicom/core.py` stays unchanged and the CLI keeps working; the app wraps it.

Two constraints that matter more than usual for this tool:

- **It handles identifiable patient data.** Anything that opens a network port, phones home, or
  writes identifiers somewhere new is a defect, not a feature.
- **The people running it are clinicians on hospital or personal laptops**, macOS and Windows,
  often with no admin rights and no Python. Long unattended runs on flaky external drives.

## Front end: Streamlit dashboard vs native window

The v0.1 dashboard is Streamlit. It was the quickest way to get a screen in front of the v0.1
engine, and it is kept for developers. It is the wrong thing to ship inside a packaged app:

| | Streamlit (v0.1 dashboard) | Native Tk window (chosen) |
|---|---|---|
| How it runs | Starts an HTTP server on a local port and opens a browser tab | Ordinary window, no server, no port |
| Network surface | Listens on a socket. Any local process can reach it; a wrong `--server.address` exposes it to the LAN. No authentication | None. The app never opens a socket |
| Telemetry | Sends usage statistics to Streamlit Inc. unless explicitly disabled | None |
| Frozen with PyInstaller | Notoriously fragile: hidden imports, metadata, static assets, and it must find a Python interpreter to spawn `python -m streamlit` | Tkinter ships with python.org Python; PyInstaller bundles it with no extra hooks |
| Bundle size | 250 MB+ (Streamlit, pandas, pyarrow, tornado) | ~40 MB (Python, Tk, pydicom) |
| Dependencies to keep patched | Dozens | pydicom only |
| Native file / folder dialogs | Not available; users type paths | Yes |
| Long-run robustness | Browser tab sleeps, reruns the script every few seconds, loses state on refresh | A worker thread and a subprocess; nothing to refresh |

Tk's weakness is looks. With the `ttk` themed widgets it uses the platform's native theme
(Aqua on macOS, Vista on Windows) and is perfectly presentable for a clinical utility. Qt
(PySide6) would look slightly better at the cost of a 150 MB dependency with its own release
cadence, and Toga (BeeWare's toolkit) is not mature enough for the table views this app needs.

**Decision: native Tkinter/ttk window.** The Streamlit dashboard stays in the repo as a
developer tool, with telemetry now switched off and bound to localhost.

## Packager: PyInstaller vs Briefcase

| | PyInstaller (chosen) | Briefcase (BeeWare) |
|---|---|---|
| What it produces | A folder (or `.app` on macOS) containing a private Python and the code. We wrap that in a `.dmg` / zip | A signed `.app` + `.dmg`, an `.msi` on Windows, using its own project template |
| Tkinter on macOS | Works: it bundles the Tk that python.org Python ships | **Does not work.** Briefcase uses BeeWare's own Python support packages for macOS, and those do not include Tk. It is built around Toga |
| Toolkits it is built for | Anything | Toga first; other toolkits are second-class |
| Maturity / community | The default choice for a decade; hooks exist for pydicom | Younger; smaller community; template churn between versions |
| Signing and notarisation | Left to us (a shell script around `codesign` / `notarytool`) | Built in, nice when it works |
| Reproducibility | A `.spec` file checked into the repo | `pyproject.toml` section; also fine |
| Re-entering the bundle as a CLI child process | Supported: `sys.executable` is the bundled program; `multiprocessing`-style re-entry works | Awkward; the bundle is an app, not a program you call with arguments |

The Tkinter point alone decides it. Briefcase would force the front end to Toga, and Toga does
not have the table, log and progress widgets this app needs without writing them ourselves.

**Decision: PyInstaller, one-folder mode**, built from python.org Python 3.12 (not conda, which
drags in a large and non-reproducible site-packages). The spec file is `packaging/scrub_dicom.spec`.
One-folder rather than one-file because one-file extracts itself to a temp folder on every launch
(slow, and it puts the program on a disk the user did not choose) and because the app re-launches
its own executable as the engine child process, which is fast and clean in one-folder mode.

## How the app drives the engine

The app does **not** import the engine into its own process and call it. It spawns the engine as a
child process, exactly as the CLI would be run:

- frozen: `Scrub-DICOM.app/Contents/MacOS/Scrub-DICOM --cli run --manifest ... --output ...`
- from source: `python -u -W ignore -m scrubdicom run --manifest ... --output ...`

Why:

- It is literally "the app wraps the CLI". Every option maps to a CLI flag and the exact command
  is shown to the user before a run starts, so anything the app does can be reproduced from a
  terminal.
- **Stop means stop.** A thread cannot be killed; a process can. Killing the engine mid-study is
  safe by design: the study has no `.complete` marker, so the next `--resume` redoes it.
- The engine's globals (skip list, log file, keep-awake handle, watchdog thread) are reset for free
  because each run is a fresh process.
- A crash in the engine cannot take the window down with it.

The `--cli` re-entry lives in `scrubdicom/app/__init__.py`. When the bundled program is started
with `--cli` as its first argument it never imports Tk; it calls `scrubdicom.core.main()` with
the remaining arguments and exits with its return code.

## Installers and signing

- **macOS**: `packaging/build_macos.sh` runs PyInstaller, then `hdiutil` to make a `.dmg` with the
  usual drag-to-Applications layout. If `SCRUBDICOM_SIGN_IDENTITY` is set it code-signs with the
  hardened runtime; if the notarisation variables are set it submits to Apple. Unsigned builds work
  but the first launch needs right-click > Open (documented in the user guide).
- **Windows**: `packaging/build_windows.bat` runs PyInstaller and zips the folder. An optional Inno
  Setup script (`packaging/windows_installer.iss`) produces a per-user installer that needs no
  admin rights. Without a code-signing certificate SmartScreen shows "unknown publisher" once.

Neither certificate is required to use the app. Both are recommended before it is given to other
centres, and `docs/security.md` says why.

## What was rejected and why, in one line each

- **Streamlit inside the bundle**: a web server with telemetry is the wrong shape for a
  confidential-data tool, and it does not freeze cleanly.
- **Briefcase**: no Tkinter on macOS; would force a Toga rewrite.
- **PySide6 / Qt**: fine, but 150 MB and a dependency that needs tracking for a UI this small.
- **pywebview + HTML**: two languages to maintain and a webview engine that differs per OS.
- **One-file PyInstaller**: slower start, self-extracts to temp, awkward for the child-process
  re-entry.
- **Running the engine in a thread**: cannot be stopped, shares global state between runs.
