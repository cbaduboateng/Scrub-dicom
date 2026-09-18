; Inno Setup script for a per-user Scrub-DICOM installer (no admin rights needed).
; Built by packaging\build_windows.bat when iscc.exe is on PATH:  iscc /DAppVersion=0.2.0 packaging\windows_installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7D1C0B0E-5E2E-4A7A-9C6D-3B0F0C5A2D11}
AppName=Scrub-DICOM
AppVersion={#AppVersion}
AppPublisher=BB & Co Holdings Ltd
AppCopyright=Built by Dr Charles Badu-Boateng. Copyright BB & Co Holdings Ltd. PolyForm Noncommercial 1.0.0.
DefaultDirName={localappdata}\Programs\Scrub-DICOM
DefaultGroupName=Scrub-DICOM
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Scrub-DICOM-{#AppVersion}-setup
SetupIconFile=icons\icon.ico
UninstallDisplayIcon={app}\Scrub-DICOM.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
DisableProgramGroupPage=yes

[Files]
Source: "..\dist\Scrub-DICOM\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Scrub-DICOM"; Filename: "{app}\Scrub-DICOM.exe"
Name: "{userdesktop}\Scrub-DICOM"; Filename: "{app}\Scrub-DICOM.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Run]
Filename: "{app}\Scrub-DICOM.exe"; Description: "Open Scrub-DICOM"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; the app's remembered paths; the user's scans and output are never touched by uninstall
Type: filesandordirs; Name: "{userappdata}\Scrub-DICOM"
