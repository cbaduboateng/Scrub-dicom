# Security and confidentiality notes

Scrub-DICOM exists to remove identity from patient scans. This page is the honest account of where
identifiable data goes while it runs, what the software will never do, and what is still the
operator's job. Read it before running the tool on real exports, and again before handing output to
anyone.

## What the software will never do

- **Modify an original file.** The engine opens source files read-only and writes copies elsewhere.
  There is no code path that writes into an input folder. (`tests/test_end_to_end.py` checks that
  the originals still hold the planted names after a run.)
- **Connect to the internet.** Neither the engine nor the desktop app imports a network module;
  `tests/test_repo_hygiene.py` fails the build if one appears. The macOS entitlements file grants no
  network entitlement. The packaged app is built without `requests`, `urllib3` or `tornado`, and the
  macOS build script refuses to produce a bundle that contains them.
- **Listen on a port.** The desktop app is a plain window. (The developer-only Streamlit dashboard
  does start a local HTTP server; since v0.2 it binds to `127.0.0.1` only and has Streamlit's usage
  telemetry switched off. It is not part of the packaged app.)
- **Send telemetry, crash reports or usage statistics.** There is nothing to send to.
- **Store patient identifiers or paths in its own settings.** The app remembers only the options you ticked
  (never a folder path: folder names are often hospital numbers), in `~/Library/Application Support/Scrub-DICOM/settings.json` (macOS) or
  `%APPDATA%\Scrub-DICOM\settings.json` (Windows). The set of keys it may write is an allow-list
  (`SETTINGS_KEYS` in `scrubdicom/app/model.py`); verify "needles" (surnames, hospital numbers) are
  deliberately excluded and are forgotten when the window closes.
- **Shift dates.** Every date becomes `19000101`. A shifted date would be recoverable by anyone who
  knew the offset; a constant is not.
- **Keep private tags.** All of them go, including the Siemens CSA header. The leaks that motivated
  this tool (referring clinician, true study datetime, age in days) were in private blocks.

## Where identifiable data lives during and after a run

| Location | Contains | Confidential? |
|---|---|---|
| Input folders | the original scans | yes, obviously |
| `<out>/<study>/…` | anonymised files | no, once verify passes |
| `<out>/_review/` | quarantined objects: secondary captures, reports, PDFs, presentation states. Headers cleaned; **pixels not touched** | treat as yes until a human has looked at each one |
| `<out>/_logs/LINKAGE_*_CONFIDENTIAL.csv` | study ID → original patient ID, name, DOB, study date, scanner | **yes**: this is the re-identification key |
| `<out>/_logs/files_*.csv`, `summary_*.csv`, `unmapped_*.csv` | original folder paths next to study IDs. Folder names are usually hospital numbers | **yes** |
| `<out>/_logs/series_*.csv` | original series descriptions, which name the vendor and sometimes the site protocol | yes |
| `<out>/_logs/app_*.txt`, `dashboard_*.txt`, `run_log*.txt` | the engine's console output, which prints source folder names | yes |
| `<out>/_logs/redactions_*.csv` | which quarantined files were released, with the box coordinates | no identifiers, but it proves a human decision was taken; keep it |
| `<out>/_logs/verify_*.txt` | PASS: file count and output path only. FAIL: quotes the offending values | PASS no; FAIL yes |
| `<out>/_logs/uid_salt.txt` | the random salt for UID hashing | no. It cannot reverse a hash. Keep it with the output if re-runs must reproduce the same UIDs |
| `<out>/<study>/.complete*` | a timestamp | no |
| App settings file | options and booleans only; no paths | no |
| The window and the app's own log lines | whatever the engine prints, including source folder names | shown to the operator only |

The rule that follows: **the whole `_logs` folder is confidential, not only the LINKAGE file.** The
app's "Logs & sharing" tab enforces this. "Move confidential logs out" relocates everything except
`uid_salt.txt` and PASS verify reports to a folder you choose outside the output tree, and "Ready to
share?" blocks while a linkage file is anywhere under the output folder or verify has not passed.

## Profiles: what may be retained, and what never is

Since 0.4.0 a run can use a de-identification profile. A profile can only retain attributes that DICOM
PS3.15 Annex E lists as retain-able options: patient characteristics (sex, age bucketed to 5 years,
weight, height), longitudinal temporal information *with modification* (dates shifted by a secret
per-patient offset derived from the UID salt; clock times kept), device identity (make and model,
technical fields) and institution identity (name only). It can also choose the patient name text, the
study description text and the method text. The default profile retains nothing and is the validated
policy.

In every profile, without exception: private tags, original UIDs, physician and operator names,
accession and order numbers, addresses, comments, other patient IDs, device serial numbers, software
versions and PACS AE titles are removed. `tests/test_profiles.py` asserts this for a profile with every
option ticked. The profile used is written into each file's DeidentificationMethod and into
`_logs/profile.json`, and `verify` checks the output against that profile rather than the default, so
a retained attribute is never mistaken for a leak and a leak is never excused by a profile.

Shifted dates: the offset is 1 to 3650 days backwards, deterministic per (salt, study ID), so a patient's
files and re-runs agree and intervals between studies survive. Without the salt the offset cannot be
recovered. The original dates remain in the linkage log and are used as verify needles.

## The operator's responsibilities

The software cannot do these for you.

1. **Run verify and read it.** Every output must pass. Add needles for anything specific to your
   cohort: consultant surnames, the hospital's ODS code, an unusual protocol name. The linkage log's
   original IDs are used as needles automatically.
2. **Look at `_review/`.** Dose reports, secondary captures and PDFs can carry burned-in text. The
   viewer's "Anonymised output" view shows each one; draw boxes over any text and release the file,
   or delete it. There is no automatic detection of burned-in text: the eye is the detector.
3. **Keep the linkage log somewhere else**, on an encrypted volume, with the same protection as the
   original scans. It is the only way back from study ID to patient.
4. **Do not rename study IDs to something meaningful.** A study ID that encodes the site or the
   diagnosis defeats the blinding.
5. **Confirm your own governance requirements.** Scrub-DICOM follows DICOM PS3.15 Annex E with the
   options described in `docs/tag_policy.md`, then removes more. It is a tool, not a legal opinion;
   your Caldicott Guardian, ethics approval or DPIA may require more or different handling.
6. **Vendor fingerprinting is reduced, not eliminated.** Kernel names, scan options and ImageType
   free text are removed by default, but pixel-level characteristics (noise texture, matrix, field of
   view) can still hint at a scanner family to an expert reader. That is acceptable for the intended
   blinded read; it is not a claim of perfect unlinkability.

## Supply chain and build integrity

- Runtime dependencies, pinned in `packaging/requirements-build.txt`: `pydicom` (engine), `openpyxl`
  (`.xlsx` mapping files), and for the viewer `numpy` and `python-gdcm` (Apache-2.0; decodes JPEG 2000,
  JPEG-LS and JPEG pixel data). Nothing else ships. GDCM bundles its own OpenSSL for DICOM attribute
  encryption; nothing in the app opens a socket, and the build refuses a bundle containing an HTTP library.
- Builds use python.org Python in a fresh virtualenv; the build scripts refuse conda.
- The app is built from the same `scrubdicom/core.py` as the CLI, with no engine changes. Every
  build runs the full test suite, then runs the frozen engine on synthetic patients and verifies the
  result before an installer is produced.
- Every release artifact gets a SHA-256 file next to it. Compare it before installing on another
  machine.
- **Code signing.** Unsigned builds work but trigger Gatekeeper / SmartScreen warnings and are
  easier to tamper with. Before giving the app to other centres, sign it: an Apple Developer ID for
  macOS (then notarise) and an Authenticode certificate for Windows. The build scripts do both when
  the credentials are provided through environment variables; nothing secret is stored in the repo.
- Antivirus: PyInstaller builds are occasionally flagged by heuristic scanners because malware also
  uses PyInstaller. UPX compression is disabled to reduce this. Signing is the real fix.

## Reporting a problem

If verify passes and you still find an identifier in an output file, that is a defect in the tag
policy. Open an issue with the DICOM tag (group, element), the VR, and where the value came from,
but **never the value itself**, and add a fixture in `scrubdicom/fixtures.py` that plants the same
kind of leak so the test suite catches it from then on.
