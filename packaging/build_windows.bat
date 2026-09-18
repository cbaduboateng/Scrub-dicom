@echo off
REM Build Scrub-DICOM.exe (one folder) and a zip on Windows. Run from a normal command prompt:
REM
REM     packaging\build_windows.bat
REM
REM Needs python.org Python 3.12 installed for the user (the "py" launcher). Makes a private build venv, runs the
REM test suite, freezes with PyInstaller, smoke-tests the frozen app, zips dist\Scrub-DICOM. If Inno Setup's
REM iscc.exe is on PATH it also builds a per-user installer (no admin rights needed to install).
REM
REM Optional Authenticode signing: set SCRUBDICOM_SIGNTOOL_ARGS to the arguments for signtool sign, e.g.
REM     set SCRUBDICOM_SIGNTOOL_ARGS=/fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /n "BB & Co Holdings Ltd"
setlocal EnableDelayedExpansion
cd /d "%~dp0\.."
set PYTHONUTF8=1

set "PY=py -3.12"
%PY% --version >nul 2>&1 || (echo Python 3.12 from python.org is not installed for this user. & exit /b 1)
%PY% -c "import sys; sys.exit(1 if 'conda' in sys.version.lower() else 0)" || (echo Refusing to build from conda Python. & exit /b 1)

set "VENV=.venv-build"
if not exist "%VENV%\Scripts\python.exe" %PY% -m venv "%VENV%" || exit /b 1
call "%VENV%\Scripts\activate.bat"
python -m pip install -q --upgrade pip
pip install -q -r packaging\requirements-build.txt -e . || exit /b 1

echo == tests
python -m pytest -q || exit /b 1

echo == icons
if not exist packaging\icons\icon.ico python packaging\icons\make_icons.py || exit /b 1

echo == pyinstaller
if exist dist\Scrub-DICOM rmdir /s /q dist\Scrub-DICOM
pyinstaller --noconfirm --clean --log-level WARN packaging\scrub_dicom.spec || exit /b 1
set "EXE=dist\Scrub-DICOM\Scrub-DICOM.exe"

echo == smoke tests on the frozen app
"%EXE%" --cli --help >nul || exit /b 1
"%EXE%" --selftest || exit /b 1
set "TMP_FX=%TEMP%\scrubdicom_smoke_%RANDOM%"
python -m scrubdicom.fixtures "%TMP_FX%\fx" >nul || exit /b 1
"%EXE%" --cli run --input "%TMP_FX%\fx\ORFAN0231" --output "%TMP_FX%\out" --study-id SMOKE-001 >nul || exit /b 1
"%EXE%" --cli verify --output "%TMP_FX%\out" --needle SMITH --needle BLOGGS | findstr /b PASS >nul || (echo frozen engine verify did not PASS & exit /b 1)
echo frozen engine: PASS
rmdir /s /q "%TMP_FX%"

for /f %%v in ('python -c "import scrubdicom; print(scrubdicom.__version__)"') do set "VERSION=%%v"

if defined SCRUBDICOM_SIGNTOOL_ARGS (
  echo == signtool
  signtool sign %SCRUBDICOM_SIGNTOOL_ARGS% "%EXE%" || exit /b 1
) else (
  echo == not signed. SmartScreen will show "unknown publisher" once; More info ^> Run anyway.
)

echo == zip
set "ZIP=dist\Scrub-DICOM-%VERSION%-windows-x64.zip"
if exist "%ZIP%" del "%ZIP%"
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\Scrub-DICOM\*' -DestinationPath '%ZIP%'" || exit /b 1
certutil -hashfile "%ZIP%" SHA256 > "%ZIP%.sha256"

where iscc >nul 2>&1 && (
  echo == installer
  iscc /Q "/DAppVersion=%VERSION%" packaging\windows_installer.iss || exit /b 1
  if defined SCRUBDICOM_SIGNTOOL_ARGS signtool sign %SCRUBDICOM_SIGNTOOL_ARGS% "dist\Scrub-DICOM-%VERSION%-setup.exe"
) || echo == Inno Setup (iscc) not on PATH; installer skipped. The zip works on its own.

echo Done: %ZIP%
endlocal
