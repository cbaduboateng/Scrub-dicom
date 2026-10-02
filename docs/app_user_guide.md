# Scrub-DICOM desktop app: user guide

Scrub-DICOM pseudonymises DICOM studies for blinded research reads. The header rules apply to any
modality; the tool was built and validated on cardiac CT, and "Coronary series only" and the
slice-thickness audit are CT-specific. Ultrasound and angiography often carry names burned into the
pixels: check those in the viewer and redact before sharing.

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

## Home

The first tab. Three buttons: **Open scans** (choose a folder and start), **See the demo**, and
**Viewer**. "This session" shows the output folder in use, how many patients are done and whether the
check passed, with **Continue**, **Share safely** and **Open output folder**. The app opens clean each
time; folders are not remembered between launches.

## The status strip

The coloured strip under the header is the one thing to watch, on every tab: **Not started**,
**Anonymising n of N**, **Anonymised, not yet safe to hand over**, **Verified, safe to hand over**, or
**Check failed, do not share**. An amber line below it means something is being kept in the output and
it is not fully blinded.

## First time? See the demo

Home or Help > **See the demo** loads two made-up patients into the ordinary four steps. Their folders are named
after them and their scans carry names, dates of birth and hospital numbers, so you can watch those disappear.
Walk through the steps yourself or press **Play it for me**. Nothing real is involved and nothing outside the
app's own folder is written.

## The tabs

Three tabs: **Home**, **Anonymise** and **Help**. The Anonymise tab is one journey of five steps: Open, Scans,
Remove, Save, Done. A fourth tab, **Details**, appears when you ask for it from the Done step. The **Dark / Light**
button in the header switches the look.

### Anonymise

Five steps, one screen each. **Next** becomes available when a step is complete, and a line under the steps says
what is still missing.

**Step 1: Open your scans.** First answer one question: **One patient** or **Several patients**. Nothing else is
shown until you do. Then choose the folder: that patient's folder, or the folder that holds all the patients.
If you said one patient and the folder holds more, the app stops and says so, because that is nearly always the
wrong folder; **Switch to several patients** carries on with all of them. Every sub-folder is searched. Only headers are read and nothing in the folder is changed. The list
shows each patient found, the Patient ID inside the scans and whether a coronary CT series was recognised.

| The folder holds | What happens |
|---|---|
| One folder per patient, or per study | Each gets its own new ID. Two scans of one patient in separate folders are two studies, each with an ID. |
| Several patients' files mixed in one folder | They are told apart by the Patient ID inside the scans. |
| An earlier output of this app | Skipped, so nothing is anonymised twice. |

If you already have a patient-list CSV or an ID spreadsheet, choose **Several patients** and tick **I already have a
patient list (CSV) or an ID spreadsheet** to use the list-driven methods instead (a single patient needs no list: choose that patient's folder):

| Choose | When |
|---|---|
| **A list of patients (CSV)** | A CSV with two columns, `source_folder` and `study_id`. Folders can be on different drives. |
| **A folder of patients + an ID spreadsheet** | One folder with a sub-folder per patient and a CSV or Excel sheet with an old-ID column and a new-ID column. |

**Step 2: Which scans do you want?**

- **Coronary CT only**: the thin contrast coronary reconstructions. Scouts, calcium scores, chest recons, X-rays
  and reports are left out. Every decision is listed afterwards on tab 2.
- **Everything**: every series is anonymised. Types that often carry burned-in text (X-ray, ultrasound,
  angiography, reports) are still set aside in `_review` for you to look at.
- **Let me choose**: tick the kinds of series to keep. A kind is the same modality, description and slice thickness
  across the patients, so one tick applies to the whole cohort. For a large or mixed cohort, type in **Find**
  ("cta", "pulm", "ct 1 mm"; every word must match) and press **Tick shown**: the many names one kind of scan has
  across hospitals are ticked together. "Coronary CT only" is a cardiac rule; a CTPA or other cohort uses this.

The line under the list says what would be kept. **Look at the images** opens the viewer, where the series of one
patient can be ticked by hand; that patient's own ticks then win over the choice on this step.

**Step 3: What should be removed?**

- **Everything that identifies the patient, the hospital or the scanner** (the default, and the policy validated on
  the 520-study cohort).
- **Everything, except what I tick here**: sex; age as a 5-year band; weight and height; dates moved by a secret
  per-patient number of days; scanner make and model; scanner technical details; hospital name. These are the
  options DICOM PS3.15 allows. An amber line appears under the status strip while anything is kept.
- **Use a saved profile**: the built-in profiles and your own. **Edit** also sets what the patient name, study
  description and method text become.

Whatever you choose, names, date of birth, hospital and NHS numbers, addresses, doctors' names, accession numbers,
comments, private vendor tags and the original UIDs are always removed. The choice is written into every file
(DeidentificationMethod) and into `_logs/profile.json`, and the output check verifies against it.

**Step 4: Save and go.**

- **New IDs** are numbered from the prefix you type (`ANON-001`, `ANON-002`...). Double-click an ID to change it,
  or press **Fill from a spreadsheet** (old ID, new ID; the old ID may be the Patient ID inside the scans or the
  folder name). If you open the same folder again with the same two destination folders, every patient keeps the
  ID it had and new patients are numbered after the last one.
- **Anonymised copies go to** the output folder: one folder per new ID. Only anonymised files and the verification
  record go here, so it can be handed over.
- **The confidential key goes to** a second folder: the list linking each new ID to the real patient, the UID salt
  and every log. It must be outside the output folder and it never leaves you. Both folders are suggested next to
  the folder you opened, under names that never include that folder's name.
- **Check the output when done** and **Skip patients already done**: leave both on.
- **Anonymise** asks once, then runs. The bar shows "patient n of N" and the files written.
- **Preview** is a dry run: it reads everything and reports what would be written, and writes nothing.
- **Stop** ends the run cleanly. The patient in progress is redone next time.
- **More**: show the command line so a colleague can reproduce the run, open the output folder or the log file,
  copy the log, show the rarely needed options (keep scanner technical details, no series sub-folders).
- If something goes wrong a card appears above the steps with one button: **Resume** after a drive disconnects,
  **Open report** after a failed check. Removing thick studies or releasing a file from quarantine without
  redaction asks you to type YES.

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

**Choosing series by hand.** In the "Original scans" view every series has a **Use** tick; the rule's
choices start ticked. Tick or untick what you want and press **Anonymise only the ticked series**: the
run keeps exactly those series for that patient and leaves other patients to the rule. **Let the rule
decide** clears it. The ticks are saved in the confidential folder, and the step-3 summary says how many
patients have ticks.

Viewing works the way other DICOM viewers do: mouse wheel or arrow keys scroll slices. The **Mouse
drag** switch sets what a drag does: window/level, zoom, or pan. The zoom slider, the + and - buttons,
cmd/ctrl + wheel and 1:1 / Fit all zoom; double-click fits; right-drag always pans; space plays cine.
kVp, mAs and CTDIvol appear in the series list and in the bottom-right annotation. Presets for coronary, soft tissue, lung and bone windows.
Axial, coronal and sagittal reformats are cut from the series once it has been loaded into memory
(a 300-slice coronary study takes a few seconds and about 300 MB). The Hounsfield value under the
cursor is shown top right. **Open in viewer app** hands the current file, or the series' folder, to a
viewer application of your choice: pick it once with "Choose viewer application..." (Bee, Horos,
Weasis, MicroDicom); the choice is remembered.

**Step 5: Done.** The run ends here by itself. One headline tells you where you stand: a green tick ("2 patients
anonymised and verified"), an amber mark (anonymised but not yet checked, or changed since the check), or a red
cross (the check failed: do not share).

- **View the scans** opens the anonymised output in the viewer. **Open the folder** opens the output folder.
- **Hand over** compares every file with the checksum list written at the check, then opens the folder. Only an
  unchanged output is handed over.
- **Certificate (PDF)** opens a one-page record of the run: how many patients and files, what was removed and what
  was kept, the result, the checksum list with its SHA-256, the new IDs, what the check does not cover, and a line
  for a signature. It carries no patient identifier. It is written into the output's `_logs` folder as soon as the
  output verifies, so it travels with it.
- **Check now** (or **Check again**) runs the check.
- Under the buttons the app lists anything that still needs a person, each with its own button: files held back
  for a look (X-rays, ultrasound, reports: **Look at them**), confidential logs inside the output (**Move them
  out**), unfinished patients.
- **What was removed** shows one of your own files in plain words: the name, numbers, dates, hospital, doctor and
  scanner, each with what it became. The same card on step 3 updates as you tick what to keep.
- **Open a different output folder** shows the results of an earlier run.

While a run is going, the line under the bar says which patient, how many files, and about how long is left. A
long run ends with a desktop notification on macOS, and the bell everywhere. **Activity log** (bottom right) shows
the engine's own line-by-line output; it is hidden unless you ask for it.

**Drag and drop.** A folder dropped anywhere on the window is opened like a chosen one. Whether it is one patient
or several is read from what the folder holds.

### Details

**Details** on the Done step opens a tab with three pages.

**Series decisions.** One row per series per patient: kept, dropped, or "needs a look" (the engine used a fallback
rule because the header lacked contrast or cardiac-phase information). Filter, search by new ID, export as CSV.

**Check report.** The full report of the newest check, and a box for extra words that must never appear in the
output: consultant surnames, the hospital's ODS code, anything specific to the cohort, separated by commas. The
original IDs in the linkage log are searched for automatically. The words you type are forgotten when you close
the app.

**Sharing checks and logs.** Every check that stands between the output and a hand-over, the files in the
output's `_logs` folder with a note on each, **Move confidential logs out**, and **Review quarantined files**.

### Help

The four steps, nine common questions with short answers, and **See the demo**. **Full reference** opens the long
version of this guide in its own window.

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
