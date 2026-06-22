; Inno Setup script for osu!collector-gui — R3D "Cherry" themed, bundles deps.
; Compiled in CI with:  iscc /DAppVersion=<x.y.z> packaging\windows-installer.iss
;
; Expects, relative to the repo root (SourceDir=.. below):
;   dist\osu-collector-gui.exe                       (PyInstaller one-file build)
;   deps\cm-cli\CollectionManager.App.Cli.exe        (+ realm-wrappers.dll)
;   deps\MicrosoftEdgeWebview2Setup.exe              (WebView2 evergreen bootstrapper)
;   deps\ndp48-web.exe                               (.NET Framework 4.8 web installer)
; CI (.github/workflows/build.yml) fetches these deps before invoking ISCC.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; All relative paths resolve from SourceDir. The .iss lives in packaging/, so
; ".." points at the repo root — where dist\, deps\ and Output\ live.
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
; R3D "Cherry" branding (base + @2x for HiDPI; Inno picks the closest).
WizardImageFile=packaging\wizard-large.bmp,packaging\wizard-large@2x.bmp
WizardSmallImageFile=packaging\wizard-small.bmp,packaging\wizard-small@2x.bmp
; Make sure a running instance is closed so its files can be replaced
; (this is what enables seamless in-app updates).
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\osu-collector-gui.exe"; DestDir: "{app}"; Flags: ignoreversion
; Bundled Collection Manager CLI — the app auto-detects it at {app}\cm-cli\.
Source: "deps\cm-cli\*"; DestDir: "{app}\cm-cli"; Flags: ignoreversion recursesubdirs createallsubdirs
; WebView2 bootstrapper: extracted to {tmp} on demand and run only if missing.
Source: "deps\MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy
; .NET Framework 4.8 web installer: run elevated only if .NET is absent/too old.
Source: "deps\ndp48-web.exe"; Flags: dontcopy

[Icons]
Name: "{group}\osu!collector-gui"; Filename: "{app}\osu-collector-gui.exe"
Name: "{group}\Uninstall osu!collector-gui"; Filename: "{uninstallexe}"
Name: "{userdesktop}\osu!collector-gui"; Filename: "{app}\osu-collector-gui.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\osu-collector-gui.exe"; Description: "Launch osu!collector-gui"; Flags: nowait postinstall skipifsilent

[Code]
const
  WV2_CLIENT = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function DotNetReady(): Boolean;
var
  rel: Cardinal;
begin
  // .NET Framework 4.7.2+ (Release >= 461808) — required by the CM CLI.
  Result := RegQueryDWordValue(HKLM,
    'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full', 'Release', rel)
    and (rel >= 461808);
end;

function WebView2Installed(): Boolean;
var
  pv: String;
begin
  // The Evergreen runtime registers a non-empty 'pv' under EdgeUpdate Clients
  // (machine-wide 64/32-bit, or per-user). Any of them counts.
  Result :=
    (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WV2_CLIENT, 'pv', pv) and (pv <> '') and (pv <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WV2_CLIENT, 'pv', pv) and (pv <> '') and (pv <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WV2_CLIENT, 'pv', pv) and (pv <> '') and (pv <> '0.0.0.0'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  rc: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;
  // .NET Framework (the CM CLI needs it). Machine-wide, so it needs elevation —
  // 'runas' raises a UAC prompt. Almost always already present on Win10/11, so
  // this rarely fires.
  if not DotNetReady() then
  begin
    WizardForm.StatusLabel.Caption := 'Installing the .NET Framework runtime...';
    ExtractTemporaryFile('ndp48-web.exe');
    ShellExec('runas', ExpandConstant('{tmp}\ndp48-web.exe'),
              '/q /norestart', '', SW_SHOW, ewWaitUntilTerminated, rc);
  end;
  // Edge WebView2 — the pywebview GUI renders in it. The evergreen bootstrapper
  // installs per-user when unelevated, so no UAC needed.
  if not WebView2Installed() then
  begin
    WizardForm.StatusLabel.Caption := 'Installing the Edge WebView2 runtime...';
    ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
    Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
         '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, rc);
  end;
end;
