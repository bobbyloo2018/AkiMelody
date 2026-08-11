; ============================================================================
;  AkiMelody — Inno Setup Installer
;  Per-user (no UAC), modern wizard, silent-update capable.
;
;  Build flow:
;    1. python build.py            -> PyInstaller --onedir -> dist\AkiMelody\
;    2. ISCC installer.iss         -> packages dist\AkiMelody\ into AkiMelody-Setup.exe
;
;  The installer installs to {localappdata}\AkiMelody (per-user, no admin).
;  Silent installs (/SILENT /VERYSILENT) auto-relaunch AkiMelody afterward.
; ============================================================================

#define MyAppVersion "1.0.3"
#ifndef BundleDir
#define BundleDir "dist\AkiMelody"
#endif

[Setup]
; App identity — change AppId per major version to allow side-by-side installs.
AppId={{AkiMelody-2026-08-05-r1}
AppVersion=1.0.3
AppVerName=AkiMelody {#MyAppVersion}
AppName=AkiMelody
AppPublisher=AkiMelody
AppPublisherURL=https://github.com/bobbyloo2018/AkiMelody
AppSupportURL=https://github.com/bobbyloo2018/AkiMelody/issues
AppUpdatesURL=https://github.com/bobbyloo2018/AkiMelody/releases

; Per-user install — no UAC prompt, no admin rights required.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Install into the user's LocalAppData so it works without admin rights and
; user data lives separately in %LOCALAPPDATA%\AkiMelody\data and is never
; part of the files packaged by this installer.
DefaultDirName={localappdata}\AkiMelody
DefaultGroupName=AkiMelody
AllowNoIcons=yes

; Use the previous install's directory if one exists (registry key below).
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousSetupType=yes
UsePreviousTasks=yes

; Output
OutputDir=dist
OutputBaseFilename=AkiMelody-Setup
Compression=lzma2/ultra64
SolidCompression=yes
InternalCompressLevel=ultra

; UI
WizardStyle=modern
WizardImageFile=assets\banner.bmp
WizardSmallImageFile=assets\icon_small.bmp
SetupIconFile=build\icon.ico

; Uninstall
UninstallDisplayIcon={app}\AkiMelody.exe
UninstallDisplayName=AkiMelody

; Architecture
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Minimum — don't force a reboot under any circumstances.
CloseApplications=yes
CloseApplicationsFilter=AkiMelody.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon"; Description: "Start AkiMelody on login"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The PyInstaller --onedir output. build.py produces dist\AkiMelody\ containing
; AkiMelody.exe + _internal\ (DLLs, packages, etc.). Recurse to grab everything.
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Ship the icon for the Start Menu / Desktop shortcuts.
Source: "build\icon.ico"; DestDir: "{app}\assets"; Flags: ignoreversion

[Icons]
Name: "{group}\AkiMelody"; Filename: "{app}\AkiMelody.exe"; IconFilename: "{app}\assets\icon.ico"
Name: "{group}\{cm:UninstallProgram,AkiMelody}"; Filename: "{uninstallexe}"; IconFilename: "{app}\assets\icon.ico"
Name: "{autodesktop}\AkiMelody"; Filename: "{app}\AkiMelody.exe"; IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon
Name: "{userstartup}\AkiMelody"; Filename: "{app}\AkiMelody.exe"; IconFilename: "{app}\assets\icon.ico"; Tasks: startupicon

[Run]
; Interactive install — optional launch checkbox (checked by default).
Filename: "{app}\AkiMelody.exe"; Description: "{cm:LaunchProgram,AkiMelody}"; Flags: nowait postinstall skipifsilent

[Registry]
; Remember the install dir so the updater + future installs can find it.
Root: HKCU; Subkey: "Software\AkiMelody"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\AkiMelody"; ValueType: string; ValueName: "Version"; ValueData: "{#MyAppVersion}"; Flags: uninsdeletekey

[UninstallDelete]
; Leave the data\ subtree (SAVED, favorites, playlists, WebView2 profile)
; untouched. Only explicitly clean generated application files here.
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\AkiMelody.exe"

[Code]
// ---------------------------------------------------------------------------
//  AkiMelody installer logic — per-user, no-UAC, silent-update capable.
// ---------------------------------------------------------------------------

var
  // Set TRUE once we've been invoked with /SILENT or /VERYSILENT so the
  // post-install step knows to auto-relaunch AkiMelody.
  IsSilentInstall: Boolean;

// Detect /SILENT /VERYSILENT by iterating ParamStr so we don't depend on
// a CommandLine variable (not exposed by the ISCC preprocessor).
function IsSilentMode(): Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to ParamCount do
  begin
    if (Pos('/SILENT', UpperCase(ParamStr(i))) > 0) or
       (Pos('/VERYSILENT', UpperCase(ParamStr(i))) > 0) then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

// Detect /SILENT /VERYSILENT early so we can branch behavior later.
function InitializeSetup(): Boolean;
begin
  IsSilentInstall := IsSilentMode();
  Result := True;
end;

// After a successful install, relaunch AkiMelody automatically on silent
// updates. On interactive installs the [Run] checkbox handles it.
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    if IsSilentInstall then begin
      // The app registers its install path under HKCU\Software\AkiMelody.
      // Launch it now so the user sees no gap in availability.
      Exec(ExpandConstant('{app}\AkiMelody.exe'), '', '', SW_SHOWNORMAL,
           ewNoWait, ResultCode);
    end;
  end;
end;
