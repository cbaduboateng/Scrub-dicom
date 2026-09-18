#!/bin/bash
# Scrub-DICOM launcher for macOS. Put this next to manifest.csv (and fai_series_pick.csv if used), set OUT, double-click.
# First time: right-click > Open, or in Terminal:  chmod +x RUN_ME.command
cd "$(dirname "$0")"
OUT="/Volumes/YourDrive/Anonymised_Output"          # <- change
MANIFEST="manifest.csv"
PICKS="fai_series_pick.csv"                          # leave as is if you have no series-pick file

if [ ! -d "$(dirname "$OUT")" ]; then
  echo "The drive holding $OUT is not mounted. Reconnect it and run this again."; read -n 1; exit 1
fi
python3 -c "import scrubdicom" 2>/dev/null || python3 -m pip install --user scrub-dicom || python3 -m pip install --user .
PICKARG=""; [ -f "$PICKS" ] && PICKARG="--series-pick $PICKS"
echo "Checking slice thickness of finished studies at $(date)"
python3 -u -W ignore -m scrubdicom thick --output "$OUT" --fix --log-file run_log.txt
echo "Starting anonymisation at $(date)"
python3 -u -W ignore -m scrubdicom run --manifest "$MANIFEST" --output "$OUT" --resume --ctca-only $PICKARG --log-file run_log.txt
echo "Verifying at $(date)"
python3 -u -W ignore -m scrubdicom verify --output "$OUT" --log-file run_log.txt
echo; echo "Finished at $(date). Press any key to close."; read -n 1
