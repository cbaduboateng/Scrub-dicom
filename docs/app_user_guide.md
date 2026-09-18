# Scrub-DICOM desktop app: user guide

For the person running the anonymisation. No Python, no terminal. If you are the developer, see
`packaging/README.md` for how the app is built.

## Install

**macOS**

1. Open the `.dmg` and drag **Scrub-DICOM** to **Applications**.
2. First launch: if the build is not signed, macOS will say the app "cannot be opened because the
   developer cannot be verified". Right-click the app > **Open** > **Open**. This is needed once.
3. If macOS asks for permission to access a folder or an external drive, allow it; the app reads the
   scans from wherever you point it and writes only to the output folder you choose.

**Windows**

1. Run `Scrub-DICOM-<version>-setup.exe` (installs for your user only; no admin rights needed), or
   unzip `Scrub-DICOM-<version>-windows-x64.zip` anywhere and run `Scrub-DICOM.exe` from inside the
   folder. Keep the whole folder together.
2. First launch of an unsigned build: SmartScreen shows "Windows protected your PC". Click **More
   info** > **Run anyway**. Once.

The app makes no changes to your system and never connects to the internet. To uninstall, delete
the app (macOS) or use Add/Remove Programs (Windows).

## The five tabs

### Run

Three ways to tell the app which study ID each patient gets:

| Choose | When |
|---|---|
| **Cohort manifest CSV** | Most cohorts. A CSV with two columns, `source_folder` and `study_id`, one patient folder per row. Folders can be on different drives. Supports **Resume** and **coronary series only**. |
| **One patient folder** | A single case. Type the study ID. |
| **Folder of patients + mapping file** | One folder with a sub-folder per patient and a spreadsheet mapping the current ID (hospital number, or the folder name) to the study ID. |

Then choose the **Output folder**. Put it on a different drive from the scans, or at least outside
them. It will contain one folder per study ID, plus `_logs` and `_review`.

Options:

- **Keep only the coronary CTA series** (on by default): drops localisers, calcium score runs, chest
  recons, lung/sharp kernels, MPRs and dose reports. Every decision is shown on the next tab.
- **Resume**: skip studies already completed; redo half-finished ones. Leave it on.
- **Keep vendor technical fields**: keeps convolution kernel and scan options. Off for a blinded
  read, because a kernel name identifies the scanner make.
- **Flat output**: no per-series sub-folders.
- **Verify after run**: runs the check automatically when the run finishes. Leave it on.

Buttons:

- **Dry run** scans and reports what would be written, and writes nothing. Do this first with a
  new manifest.
- **Start run** shows you the exact command it is about to run, then runs it. The bar shows
  "Study n of N" with an estimate of time left; the log updates live.
- **Stop** ends the run cleanly. The study in progress is redone on the next run with Resume on.
- **Show command** displays the command line for the current settings, so a colleague can reproduce
  the run from a terminal.

The computer is kept awake for the length of the run. You can close the laptop lid on a Mac only if
it is plugged in; better to leave it open.

### Series decisions

One row per series per study, from the coronary-only rule: **kept**, **dropped**, or **needs a
look**. "Needs a look" means the engine used a fallback rule (no contrast agent or cardiac phase in
the header, or a series-pick that did not match) and a human should confirm the right series was
kept. Filter, search by study ID, and export as CSV for a colleague.

### Verify

Re-opens every output file and fails on any private tag, real date, original UID, vendor or
institution text, person name, or any extra word you type. Type consultant surnames, the hospital's
ODS code, anything specific to the cohort, separated by commas. These words are not remembered
after you close the app. The result is PASS or FAIL with the exact file and tag for each finding.

**Do not share output that has not passed.**

### Logs & sharing

The `_logs` folder is confidential, not just the LINKAGE file. The per-file and per-study CSVs and
the run logs all record the original folder paths, and folders are usually named by hospital
number.

- **Ready to share?** lists what still stands between the output folder and a reader: verify status,
  half-finished studies, quarantined files in `_review`, linkage or other confidential logs still
  inside the tree.
- **Move confidential logs out** moves everything except `uid_salt.txt` (needed so a re-run
  produces the same UIDs) and PASS verify reports to a folder you choose outside the output tree.
  Keep that folder with the linkage information, on an encrypted drive, away from the scans.
- **Open _review folder**: look at every file there before deciding whether to share it. They are
  secondary captures, reports and PDFs whose headers are clean but whose pixels may carry burned-in
  text. The safe default is to delete them from what you share.

### Help

This guide, in short form, plus the version and licence.

## Tools menu (under Run)

- **Audit slice thickness** lists finished studies whose kept series are thicker than the rule
  allows (0.8 mm, i.e. 0.75 mm and thinner are kept).
- **Audit and clear thick studies** deletes those study folders from the *output* tree so the next
  Resume run redoes them under the current rule. Originals are never touched. You are asked to
  confirm.

## When things go wrong

| Symptom | What to do |
|---|---|
| "Output location is no longer reachable" | The external drive disconnected. Reconnect it, tick Resume, Start run. Completed studies are kept. |
| "No progress for N s while reading: <file>" | A failing drive is hanging on that file. Stop. Create `skip_files.txt` next to the manifest with that full path on one line. Run again with Resume. The study is flagged CHECK because it is missing a slice. |
| "NO CORONARY SERIES FOUND" for a study | The export has no series that meets the rule (thin, cardiac FOV, >= 100 images, contrast). Re-export from PACS with the thin coronary recon, or check the Series decisions tab to see what was dropped and why. |
| Verify FAILS | Read the findings on the Verify tab. Each names the file and tag. If it is a needle you typed (e.g. a surname that is also a common word), refine the needle. If it is a real leak, do not share; report it (see `docs/security.md`). |
| The window says "finished with problems" | Read the log; the last lines say what. Exit code 1 after a drive loss just means "re-run with Resume". |
| Manifest written on a Mac, running on Windows | Fill in **Path remap**, e.g. `/Volumes/Drive=E:\`. Folder names with a trailing space are handled. |

## What the app does to every file

New PatientName and PatientID = study ID; date of birth, sex, age, addresses, other IDs removed;
every date and time set to 1900-01-01 11:11:11; every UID regenerated deterministically so a study
stays one study; all private tags removed; institution, station, manufacturer, model, physicians,
descriptions, protocol, kernel and scan options cleared; PACS AE titles dropped from the file
header. Full list and rationale: `docs/tag_policy.md`.
