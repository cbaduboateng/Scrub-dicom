"""PyInstaller entry point. Identical to `python -m scrubdicom.app`; kept as a file because PyInstaller wants a
script, not a module. `--cli ...` runs the engine without importing Tk (see scrubdicom.app.main)."""
import sys

from scrubdicom.app import main

sys.exit(main())
