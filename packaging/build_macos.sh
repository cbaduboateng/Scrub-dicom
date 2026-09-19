#!/bin/bash
# Build Scrub-DICOM.app and a .dmg on macOS. Run from anywhere:
#
#     bash packaging/build_macos.sh
#
# Uses python.org Python (PYTHON=/path/to/python3.12 to override), makes a private build venv, runs the test
# suite, freezes with PyInstaller, smoke-tests the frozen app, then wraps it in a drag-to-Applications .dmg.
#
# Optional signing / notarisation, read from the environment (never from the repo):
#     SCRUBDICOM_SIGN_IDENTITY="Developer ID Application: BB & Co Holdings Ltd (TEAMID)"
#     SCRUBDICOM_NOTARY_PROFILE="scrubdicom"      # a keychain profile made with: xcrun notarytool store-credentials
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 /opt/homebrew/bin/python3.12 python3; do
    command -v "$c" >/dev/null 2>&1 && PY="$c" && break
  done
fi
echo "Using $PY ($("$PY" --version))"
"$PY" - <<'EOF'
import sys
bad = "conda" in sys.version.lower() or "anaconda" in sys.prefix.lower() or "miniconda" in sys.prefix.lower()
sys.exit("Refusing to build from a conda Python: use python.org or Homebrew Python (PYTHON=/path/to/python3.12)." if bad else 0)
EOF

VENV=.venv-build
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install -q --upgrade pip
# hash-pinned: every wheel must match packaging/requirements-build.lock (regenerate with: pip-compile --generate-hashes packaging/requirements-build.in)
if [ -f packaging/requirements-build.lock ]; then
  pip install -q --require-hashes -r packaging/requirements-build.lock
else
  echo "WARNING: packaging/requirements-build.lock missing; installing pinned versions WITHOUT hash verification" >&2
  pip install -q -r packaging/requirements-build.in
fi
pip install -q --no-deps -e .
echo "== dependency audit"
python -m pip_audit --strict --desc -r packaging/requirements-build.in || { echo "pip-audit found known vulnerabilities" >&2; exit 1; }

echo "== tests"
python -m pytest -q

echo "== icons"
[ -f packaging/icons/icon.icns ] || python packaging/icons/make_icons.py

echo "== pyinstaller"
rm -rf build/Scrub-DICOM dist/Scrub-DICOM dist/Scrub-DICOM.app
pyinstaller --noconfirm --clean --log-level WARN packaging/scrub_dicom.spec
APP="dist/Scrub-DICOM.app"
BIN="$APP/Contents/MacOS/Scrub-DICOM"

echo "== smoke tests on the frozen app"
"$BIN" --cli --help >/dev/null
"$BIN" --selftest
# the frozen engine must anonymise and verify synthetic patients exactly as the source CLI does
TMP="$(mktemp -d)"
python -m scrubdicom.fixtures "$TMP/fx" >/dev/null
"$BIN" --cli run --input "$TMP/fx/ORFAN0231" --output "$TMP/out" --study-id SMOKE-001 >/dev/null
"$BIN" --cli verify --output "$TMP/out" --needle SMITH --needle BLOGGS | grep -q '^PASS' && echo "frozen engine: PASS"
rm -rf "$TMP"
# nothing in the bundle may be able to reach the network: no requests/urllib3/tornado
if find "$APP" -iname 'requests' -o -iname 'urllib3*' -o -iname 'tornado' | grep -q .; then
  echo "unexpected package inside the bundle" >&2; exit 1
fi
# the viewer must be able to decode compressed pixel data in the frozen app
"$BIN" --cli-check-decoders | grep -q "gdcm" || { echo "GDCM decoder missing from the bundle" >&2; exit 1; }

VERSION="$(python -c 'import scrubdicom; print(scrubdicom.__version__)')"
# numpy / GDCM wheels are single-architecture, so the app runs on the architecture it was built on
ARCH="$(uname -m)"

# codesign refuses a bundle carrying Finder / file-provider attributes, which folders under iCloud or Documents sync
# add to every file. Stage a clean copy (ditto drops them), sign that, and build the dmg from it.
STAGE="$(mktemp -d)"
SAPP="$STAGE/Scrub-DICOM.app"
ditto --norsrc --noextattr --noqtn "$APP" "$SAPP"
if [ -n "${SCRUBDICOM_SIGN_IDENTITY:-}" ]; then
  echo "== codesign (Developer ID, hardened runtime)"
  codesign --deep --force --options runtime --timestamp --entitlements packaging/entitlements.plist \
           --sign "$SCRUBDICOM_SIGN_IDENTITY" "$SAPP"
else
  echo "== ad-hoc signature only (set SCRUBDICOM_SIGN_IDENTITY for a Developer ID). First launch needs right-click > Open."
  codesign --deep --force --sign - "$SAPP"
fi
codesign --verify --deep --strict "$SAPP" && echo "signature verified"
"$SAPP/Contents/MacOS/Scrub-DICOM" --cli --help >/dev/null   # signed copy still runs
rm -rf "$APP"; ditto --norsrc --noextattr --noqtn "$SAPP" "$APP"   # keep the signed bundle in dist/ too

echo "== dmg"
DMG="dist/Scrub-DICOM-$VERSION-macOS-$ARCH.dmg"
rm -f "$DMG"
ln -s /Applications "$STAGE/Applications"
cp docs/app_user_guide.md "$STAGE/READ ME FIRST.md"
hdiutil create -quiet -volname "Scrub-DICOM $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
hdiutil verify -quiet "$DMG" && echo "dmg verified"
rm -rf "$STAGE"

if [ -n "${SCRUBDICOM_SIGN_IDENTITY:-}" ]; then
  echo "== sign the disk image"
  codesign --force --timestamp --sign "$SCRUBDICOM_SIGN_IDENTITY" "$DMG"
  codesign --verify --verbose=2 "$DMG"
fi
if [ -n "${SCRUBDICOM_SIGN_IDENTITY:-}" ] && [ -n "${SCRUBDICOM_NOTARY_PROFILE:-}" ]; then
  echo "== notarise"
  xcrun notarytool submit "$DMG" --keychain-profile "$SCRUBDICOM_NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
  echo "== Gatekeeper assessment of the disk image"
  spctl -a -vv -t open --context context:primary-signature "$DMG" 2>&1 | tail -2
fi

# software bill of materials: what is inside the bundle, with versions and licences
python - "$VERSION" "$ARCH" > "dist/Scrub-DICOM-$VERSION-macOS-$ARCH-sbom.json" <<'PY'
import json, sys, importlib.metadata as md, time
pkgs = []
for d in sorted(md.distributions(), key=lambda d: d.metadata["Name"].lower()):
    m = d.metadata
    pkgs.append({"name": m["Name"], "version": m["Version"], "license": (m.get("License-Expression") or m.get("License") or "")[:80]})
print(json.dumps({"bomFormat": "scrub-dicom-simple", "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "app": "Scrub-DICOM",
                  "version": sys.argv[1], "platform": f"macOS-{sys.argv[2]}", "components": pkgs}, indent=2))
PY
shasum -a 256 "$DMG" | tee "$DMG.sha256"
du -sh "$APP" "$DMG"
echo "Done: $DMG"
