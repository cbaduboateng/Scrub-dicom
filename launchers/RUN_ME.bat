@echo off
REM Scrub-DICOM launcher for Windows. Put this next to manifest.csv (and fai_series_pick.csv if used), set OUT, double-click.
setlocal
cd /d "%~dp0"
set "OUT=E:\Anonymised_Output"                       & REM <- change
set "MANIFEST=manifest.csv"
set "PICKS=fai_series_pick.csv"
set "REMAP=/Volumes/YourDrive=E:\"                   & REM <- only needed if the manifest was written on a Mac
set PYTHONUTF8=1

for %%D in ("%OUT%") do if not exist "%%~dD\" (
  echo The drive for %OUT% is not connected. Plug it in and run this again. & pause & exit /b 1
)
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Python is not installed. Install it from https://www.python.org/downloads/windows/ - tick "Add python.exe to PATH" - then run this again.
  pause & exit /b 1
)
%PY% -c "import scrubdicom" >nul 2>&1 || %PY% -m pip install --user scrub-dicom || %PY% -m pip install --user .
set "PICKARG="
if exist "%PICKS%" set "PICKARG=--series-pick %PICKS%"
echo Checking slice thickness of finished studies at %date% %time%
%PY% -u -W ignore -m scrubdicom thick --output "%OUT%" --fix --log-file run_log.txt
echo Starting anonymisation at %date% %time%
%PY% -u -W ignore -m scrubdicom run --manifest "%MANIFEST%" --output "%OUT%" --resume --ctca-only %PICKARG% --remap "%REMAP%" --log-file run_log.txt
echo Verifying at %date% %time%
%PY% -u -W ignore -m scrubdicom verify --output "%OUT%" --log-file run_log.txt
echo.
echo Finished at %date% %time%. You can close this window.
pause
