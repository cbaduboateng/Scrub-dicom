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

## The four steps

The tabs are numbered in the order you use them. The **Dark / Light** button in the header switches the
look; the app follows the system appearance at first launch. Under the buttons on the first tab a line
tells you what is still missing, and Anonymise becomes available when the form is complete.

### 1  Anonymise

**Step 1: where the scans are, and what each patient will be called.**

| Choose | When |
|---|---|
| **A list of patients (CSV)** | Most cohorts. A CSV with two columns, `source_folder` (the folder holding that patient's scans) and `study_id` (the new ID). Folders can be on different drives. Can be stopped and resumed, and can keep only the coronary series. |
| **One patient** | A single case. Type the new ID. |
| **A folder of patients + an ID spreadsheet** | One folder with a sub-folder per patient and a CSV or Excel sheet with an old-ID column and a new-ID column. Say whether the old ID is the Patient ID inside the scans or the sub-folder name. |

**Step 2: where the anonymised copies go.** A different drive from the scans, or at least a
folder outside them. It will contain one folder per patient, named by new ID, plus `_logs` and
`_review`.

**Step 3: options.**

- **Keep only the coronary CT angiogram series** (on by default): drops scouts, calcium score runs,
  chest recons, lung/sharp kernels, MPRs and dose reports. Every decision is shown on tab 2.
- **Skip patients already done (resume)**: leave on. Half-finished patients are redone.
- **Keep scanner technical details**: kernel and scan options. Leave off for a blinded read, because
  a kernel name identifies the scanner make.
- **One folder per patient, no series sub-folders**.
- **Check the output automatically when done**: leave on.

**De-identification profile.** "Blinded read" (the default) removes everything identifying and is the
policy validated on the 520-study cohort. Other profiles keep a little more when a study needs it:

| Profile | Keeps |
|---|---|
| Blinded read (default) | nothing |
| Longitudinal follow-up | sex, age in 5-year buckets, dates shifted by a secret per-patient offset so intervals survive |
| Blinded read + patient characteristics | sex, 5-year age bucket, weight and height |
| Blinded read + scanner and technical | scanner make and model, kernel and scan options |

**Edit profiles...** lets you copy a built-in profile and tick exactly what to keep, and set what the
patient name, study description and method text become. Whatever you tick, private tags, original UIDs,
physician names, accession and order numbers, comments and addresses are always removed. The profile
is written into every file (DeidentificationMethod) and into `_logs/profile.json`, and the output check
verifies against it, so a reader can tell what was retained. The viewer's header before/after follows
the chosen profile.

Buttons:

- **Preview (writes nothing)**: scans and reports what would be written. Do this first with a new
  list.
- **Anonymise**: shows you the exact command it is about to run, then runs it. The bar shows
  "Patient n of N" with an estimate of time left; the log updates live.
- **Stop**: ends the run cleanly. The patient in progress is redone next time.
- **Show the command this will run**: so a colleague can reproduce the run from a terminal.

The computer is kept awake for the length of the run. Leave a laptop open and plugged in.

### The viewer

Open it from tab 1 ("Preview a patient in the viewer..."), tab 2 ("Open patient in viewer") or tab 4
("Review and redact quarantined files in the viewer..."). It has two views of the same window:

- **Original scans**: what Anonymise *will* do, computed in memory. Every series with its keep / drop
  decision and a thumbnail; the images; and the header before and after, with each removed, changed
  or added tag highlighted. Nothing is written to disk. **Keep this series instead** records your
  choice in `series_picks.csv` next to the patient list, and the run honours it.
- **Anonymised output**: what *was* written, plus the quarantined files in `_review`. Tick
  **Redact**, drag boxes over burned-in text, then **Apply and release**: the boxes are painted black
  in the anonymised copy, the file moves into the patient's folder, and the action is logged. Run the
  output check again afterwards; the share checklist reminds you.

Viewing works the way other DICOM viewers do: mouse wheel or arrow keys scroll slices. The **Mouse
drag** switch sets what a drag does: window/level, zoom, or pan. The zoom slider, the + and - buttons,
cmd/ctrl + wheel and 1:1 / Fit all zoom; double-click fits; right-drag always pans; space plays cine.
kVp, mAs and CTDIvol appear in the series list and in the bottom-right annotation. Presets for coronary, soft tissue, lung and bone windows.
Axial, coronal and sagittal reformats are cut from the series once it has been loaded into memory
(a 300-slice coronary study takes a few seconds and about 300 MB). The Hounsfield value under the
cursor is shown top right. **Open this file in your DICOM viewer** hands the current file to
whatever viewer is installed on the computer (Bee, Horos, Weasis, MicroDicom).

### 2  Check series

One row per series per patient: **kept**, **dropped**, or **needs a look**. "Needs a look" means the
engine used a fallback rule (no contrast agent or cardiac phase in the header, or an
already-analysed pick that did not match) and a human should confirm the right series was kept.
Filter, search by new ID, and export as CSV for a colleague.

### 3  Verify output

Re-opens every output file and fails on any private tag, real date, original UID, vendor or
institution text, person name, or any extra word you type. Type consultant surnames, the hospital's
ODS code, anything specific to the cohort, separated by commas. These words are forgotten when you
close the app. The result is PASS or FAIL with the exact file and tag for each finding.

**Do not share output that has not passed.**

### 4  Share safely

The `_logs` folder is confidential, not just the LINKAGE file. The per-file and per-patient CSVs and
the run logs all record the original folder paths, and folders are usually named by hospital
number.

- **Is the output folder safe to hand over?** lists what still stands between the output folder and
  a reader: check status, half-finished patients, quarantined files in `_review`, linking or other
  confidential logs still inside the tree.
- **Move the patient-linking logs out** moves everything except `uid_salt.txt` (needed so a re-run
  produces the same UIDs) and passed check reports to a folder you choose outside the output tree.
  Keep that folder with the linkage information, on an encrypted drive, away from the scans.
- **Open quarantined files (_review)**: look at every file there before deciding whether to share it.
  They are secondary captures, reports and PDFs whose headers are clean but whose pixels may carry
  burned-in text. The safe default is to delete them from what you share.

### Help

This guide in short form, plus the version and licence.

## Actions menu

- **List patients whose kept slices are too thick**: the rule is 0.8 mm (0.75 mm and thinner are
  kept).
- **Remove those patients from the output so they are redone**: deletes their folders from the
  *output* tree only, so the next run with "Skip patients already done" redoes them. Originals are
  never touched. You are asked to confirm.

## When things go wrong

| Symptom | What to do |
|---|---|
| "Output location is no longer reachable" | The external drive disconnected. Reconnect it, tick "Skip patients already done", click Anonymise. Completed patients are kept. |
| "No progress for N s while reading: <file>" | A failing drive is hanging on that file. Stop. Create `skip_files.txt` next to the patient list with that full path on one line. Run again. The patient is flagged "needs a look" because a slice is missing. |
| "NO CORONARY SERIES FOUND" for a patient | The export has no series that meets the rule (thin, cardiac field of view, 100+ images, contrast). Re-export from PACS with the thin coronary recon, or look at tab 2 to see what was dropped and why. |
| The check FAILS | Read the findings on tab 3. Each names the file and tag. If it is a word you typed that is also a common word, refine it. If it is a real leak, do not share; report it (see `docs/security.md`). |
| The window says "finished with problems" | Read the log; the last lines say what. Exit code 1 after a drive loss just means "run again with skip-already-done ticked". |
| Patient list written on a Mac, running on Windows | Fill in **Drive path fix**, e.g. `/Volumes/Drive=E:\`. Folder names with a trailing space are handled. |

## What the app does to every file

New PatientName and PatientID = the new ID; date of birth, sex, age, addresses, other IDs removed;
every date and time set to 1900-01-01 11:11:11; every UID regenerated deterministically so a study
stays one study; all private tags removed; institution, station, manufacturer, model, physicians,
descriptions, protocol, kernel and scan options cleared; PACS AE titles dropped from the file
header. Full list and rationale: `docs/tag_policy.md`.
