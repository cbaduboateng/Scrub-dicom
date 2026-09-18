# Changelog

## 0.1.0 (2026-09-18)
First public release, extracted from the SCAD reader-study pipeline.
- `run` (single folder / mapping file / manifest), `verify`, `thick`
- `--ctca-only` coronary-series selection with per-series decision log; `--series-pick` for pre-analysed series (thin-slice override)
- resumable runs with completion markers; clean stop on drive loss; read-hang watchdog; `skip_files.txt`
- built-in keep-awake (macOS caffeinate, Windows power request)
- Windows: `--remap` for Mac-written manifests, long-path and trailing-space folder handling
- Streamlit dashboard
