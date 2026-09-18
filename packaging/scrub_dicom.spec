# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Scrub-DICOM desktop app. Build with:

    pyinstaller --noconfirm --clean packaging/scrub_dicom.spec

from the repository root, inside a virtualenv made from python.org Python (see packaging/README.md). Produces
dist/Scrub-DICOM.app on macOS and dist/Scrub-DICOM/ on Windows and Linux. One-folder mode: the app re-launches its
own executable with --cli as the engine child process, which is fast and needs no self-extraction.
"""
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent          # SPECPATH is set by PyInstaller to this file's folder
sys.path.insert(0, str(ROOT))
from scrubdicom.core import VERSION             # noqa: E402

NAME = "Scrub-DICOM"
ICON = ROOT / "packaging" / "icons" / ("icon.icns" if sys.platform == "darwin" else "icon.ico")

from PyInstaller.utils.hooks import collect_all
gdcm_datas, gdcm_binaries, gdcm_hidden = collect_all("gdcm")
sv_datas, sv_binaries, sv_hidden = collect_all("sv_ttk")

if sys.platform == "darwin":
    # The bootloader and python.org's Python are universal2 and are built that way (a single-architecture
    # bootloader gets quarantined by endpoint-security software during the build on managed Macs). numpy and
    # GDCM only ship single-architecture wheels, so the finished app runs on the architecture it was built on;
    # packaging/build_macos.sh names the .dmg accordingly. Let those binaries through instead of failing.
    import PyInstaller.utils.osx as _osx
    _strict = _osx.binary_to_target_arch

    def _lenient(filename, target_arch, display_name=None):
        try:
            _strict(filename, target_arch, display_name)
        except _osx.IncompatibleBinaryArchError:
            print(f"note: {display_name or filename} is single-architecture; app will run on this Mac's architecture only")
    _osx.binary_to_target_arch = _lenient

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=gdcm_binaries + sv_binaries,
    datas=[(str(ROOT / "scrubdicom" / "app" / "HELP.txt"), "scrubdicom/app")] + gdcm_datas + sv_datas,
    hiddenimports=["scrubdicom.core", "scrubdicom.app.ui", "scrubdicom.app.model", "scrubdicom.app.runner",
                   "scrubdicom.app.preview", "scrubdicom.app.viewer", "scrubdicom.app.profile_ui", "scrubdicom.app.theme", "scrubdicom.profiles", "openpyxl", "numpy", "gdcm",
                   "pydicom.pixels.decoders.gdcm", "pydicom.pixels.decoders.rle", "sv_ttk"] + gdcm_hidden + sv_hidden,
    hookspath=[],
    runtime_hooks=[],
    # The engine needs pydicom only; the viewer adds numpy and GDCM (Apache-2.0) for compressed pixel data.
    # Everything below is a test/dev dependency or an optional extra with its own network-capable code.
    excludes=["streamlit", "pandas", "pyarrow", "matplotlib", "PIL", "IPython", "jupyter", "pytest", "scipy",
              "tornado", "altair", "pylibjpeg", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "test", "unittest",
              "setuptools", "pip", "_pytest", "numpy.testing", "numpy.f2py", "numpy.distutils"],
    noarchive=False,
    optimize=0,
)
# pydicom ships ~3 MB of sample DICOM files for its own tests; the app never reads them.
a.datas = [d for d in a.datas if "pydicom/data/test_files" not in d[0].replace("\\", "/") and "pydicom/data/charset_files" not in d[0].replace("\\", "/")]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX-packed binaries trip antivirus heuristics on hospital PCs
    console=False,                  # no console window; the app shows engine output itself
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="universal2" if sys.platform == "darwin" else None,   # one build for Intel and Apple silicon; also avoids a codesign --remove failure on lipo-thinned slices
    codesign_identity=None,         # signing is done by packaging/build_macos.sh so it can also notarise
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
    version=None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        icon=str(ICON) if ICON.exists() else None,
        bundle_identifier="com.bbandcoconsulting.scrub-dicom",
        version=VERSION,
        info_plist={
            "CFBundleName": NAME,
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "Built by Dr Charles Badu-Boateng. Copyright BB & Co Holdings Ltd. PolyForm Noncommercial 1.0.0.",
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.medical",
            "NSRequiresAquaSystemAppearance": False,
        },
    )
