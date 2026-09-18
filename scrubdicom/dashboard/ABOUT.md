**Scrub-DICOM** removes patient, centre and scanner identity from cardiac CT DICOM exports so that scans from
different hospitals and diagnoses can be read blind. It replaces the manual TeraRecon / DICOM Anonymizer Pro
workflow with a scripted, resumable, self-verifying batch job.

What every file gets: new PatientName/PatientID = study ID; DOB, sex, age, addresses, other IDs removed;
every date/time dummied to 1900-01-01 11:11:11; every UID regenerated (deterministically, so a study stays
one study); all private tags removed; institution, station, manufacturer, model, physicians, descriptions,
protocol, kernel and scan options cleared; AE titles dropped from the file header.

`--ctca-only` keeps just the coronary reconstruction (thin slices, cardiac field of view, contrast) and
logs every decision. `verify` re-opens every output file and fails on anything left behind.

Built by Charles Badu-Boateng. Copyright BB & Co Holdings Ltd. PolyForm Noncommercial 1.0.0.
