; Inno Setup script: one-click installer for terra-desk.
; Installs for the current user only (no admin prompt), into %LOCALAPPDATA%\Programs\terra-desk, with a Start menu
; entry, an optional desktop shortcut and an uninstaller. Your settings, key and workspace are not touched by
; uninstalling.
;   iscc /DAppVersion=1.0.0 packaging\terra-desk.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B0C3F1E-6E4B-4C8B-9E1D-2B7A5C9E7D31}
AppName=terra-desk
AppVersion={#AppVersion}
AppPublisher=terra-desk
DefaultDirName={localappdata}\Programs\terra-desk
DefaultGroupName=terra-desk
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=terra-desk-setup
SetupIconFile=terra-desk.ico
UninstallDisplayIcon={app}\terra-desk.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on the desktop"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\terra-desk\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\build\mingit\*"; DestDir: "{app}\mingit"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

[Icons]
Name: "{group}\terra-desk"; Filename: "{app}\terra-desk.exe"
Name: "{group}\Uninstall terra-desk"; Filename: "{uninstallexe}"
Name: "{userdesktop}\terra-desk"; Filename: "{app}\terra-desk.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\terra-desk.exe"; Description: "Start terra-desk"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c taskkill /f /im terra-desk.exe"; Flags: runhidden; RunOnceId: "StopTerraDesk"
