; Inno Setup script for osu!collector-gui.
; Compiled in CI with:  iscc /DAppVersion=<x.y.z> packaging\windows-installer.iss
; Expects dist\osu-collector-gui.exe to already exist (built by PyInstaller).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; All relative paths below resolve from SourceDir. The .iss lives in
; packaging/, so ".." points at the repo root — where dist\ and Output\ live.
SourceDir=..
AppId={{B7B3E4B2-0C2E-4D7A-9E2E-7A1C0F3D5E92}}
AppName=osu!collector-gui
AppVersion={#AppVersion}
AppPublisher=Red
AppPublisherURL=https://github.com/R3dWolfie/Osu-Collector-GUI
DefaultDirName={localappdata}\osu-collector-gui
DefaultGroupName=osu!collector-gui
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=osu-collector-gui-Setup
SetupIconFile=packaging\icon.ico
UninstallDisplayIcon={app}\osu-collector-gui.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Make sure a running instance is closed so its files can be replaced
; (this is what enables seamless in-app updates).
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\osu-collector-gui.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\osu!collector-gui"; Filename: "{app}\osu-collector-gui.exe"
Name: "{group}\Uninstall osu!collector-gui"; Filename: "{uninstallexe}"
Name: "{userdesktop}\osu!collector-gui"; Filename: "{app}\osu-collector-gui.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\osu-collector-gui.exe"; Description: "Launch osu!collector-gui"; Flags: nowait postinstall skipifsilent
