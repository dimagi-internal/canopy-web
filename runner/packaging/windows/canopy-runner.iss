; Canopy Runner — per-user Windows installer (Inno Setup 6).
;
; Built by runner/packaging/windows/build_installer.ps1, which passes:
;   /DPayloadDir=<build_payload.py output for windows-x86_64>
;   /DSha12=<first 12 of the runner sha>   /DAppVersion=1.0.<committed_at>
;   /DOutputDir=<where the .exe goes>
;
; Layout (docs/superpowers/specs/2026-10-19-runner-installers-design.md):
;   %LOCALAPPDATA%\Programs\Canopy Runner\versions\<sha12>\   one complete runtime per release
;   %LOCALAPPDATA%\Programs\Canopy Runner\current            junction -> versions\<sha12>
; The Scheduled Tasks `canopy-runner setup` registers run current\bin\..., so an update
; is: install the new version beside the running one, flip the junction, restart.
;
; No admin, anywhere: PrivilegesRequired=lowest, a per-user install dir, an HKCU
; uninstall entry, and junctions (which, unlike symlinks, need no privilege).
;
;   Canopy-Runner-Setup-windows-x86_64.exe            interactive: installs, then runs
;                                                     `canopy-runner setup` in a console
;                                                     (it pairs this PC in the browser)
;   Canopy-Runner-Setup-windows-x86_64.exe /VERYSILENT /UPDATE
;                                                     what `canopy-runner self-update` runs:
;                                                     installs + flips `current`, no setup
;                                                     (the updater restarts the runner itself)

#ifndef PayloadDir
  #error PayloadDir is required (/DPayloadDir=...)
#endif
#ifndef Sha12
  #error Sha12 is required (/DSha12=...)
#endif
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

[Setup]
; Stable forever: Windows keys the "Installed apps" entry and upgrades on it.
AppId={{8CC89D05-FE52-4917-A412-02E5CDD0FE14}
AppName=Canopy Runner
AppVersion={#AppVersion} ({#Sha12})
AppVerName=Canopy Runner {#AppVersion}
AppPublisher=Dimagi
AppPublisherURL=https://canopy.dimagi.com/
VersionInfoVersion=1.0.0.0
VersionInfoDescription=Canopy Runner installer ({#Sha12})
DefaultDirName={localappdata}\Programs\Canopy Runner
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
UsedUserAreasWarning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=Canopy-Runner-Setup-windows-x86_64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Canopy Runner
; The runner runs from versions\<old>; this install never writes there, so there is
; nothing to close — and the Restart Manager would offer to kill the runner.
CloseApplications=no
RestartApplications=no
SetupLogging=yes

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}\versions\{#Sha12}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
; The final step of a person's install: pair this PC and register the tasks. A
; visible console because `setup` prints the pairing code and opens the browser;
; `|| pause` keeps the window up when it fails so the error can be read.
Filename: "{cmd}"; Parameters: "/c """"{app}\current\bin\canopy-runner.cmd"" setup || pause"""; \
  WorkingDir: "{app}"; StatusMsg: "Setting up Canopy Runner — approve this PC in your browser..."; \
  Flags: waituntilterminated; Check: NotUpdate

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/End /TN ""\Canopy\canopy-runner"""; Flags: runhidden; RunOnceId: "EndRunner"
Filename: "{sys}\schtasks.exe"; Parameters: "/End /TN ""\Canopy\canopy-runner-updater"""; Flags: runhidden; RunOnceId: "EndUpdater"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""\Canopy\canopy-runner"" /F"; Flags: runhidden; RunOnceId: "DeleteRunner"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""\Canopy\canopy-runner-updater"" /F"; Flags: runhidden; RunOnceId: "DeleteUpdater"

[UninstallDelete]
; Every version (including ones a later /UPDATE installed and this uninstaller never
; recorded), and the marker below. ~\.canopy (the runner's config and key) is the
; person's, and stays.
Type: filesandordirs; Name: "{app}\versions"
Type: files; Name: "{app}\current-version.txt"
Type: dirifempty; Name: "{app}"

[Code]
var
  PreviousSha: String;

{ /UPDATE: the runner's self-update is driving this install and runs setup itself. }
function IsUpdate: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/UPDATE') = 0 then
      Result := True;
end;

function NotUpdate: Boolean;
begin
  Result := not IsUpdate;
end;

function RunCmd(const Args: String): Integer;
begin
  if not Exec(ExpandConstant('{cmd}'), '/c ' + Args, '', SW_HIDE, ewWaitUntilTerminated, Result) then
    Result := -1;
end;

{ Remove every version but the new one and the one `current` named before this
  install (which may still be running — it is the one a self-update runs from).
  A version still in use fails to delete and is retried by the next install. }
procedure PruneVersions;
var
  Rec: TFindRec;
  Root: String;
begin
  Root := ExpandConstant('{app}\versions\');
  if FindFirst(Root + '*', Rec) then
  try
    repeat
      if (Rec.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0) and (Rec.Name <> '.') and (Rec.Name <> '..')
         and (CompareText(Rec.Name, '{#Sha12}') <> 0) and (CompareText(Rec.Name, PreviousSha) <> 0) then
      begin
        Log('Pruning old version ' + Rec.Name);
        DelTree(Root + Rec.Name, True, True, True);
      end;
    until not FindNext(Rec);
  finally
    FindClose(Rec);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Prev: AnsiString;
  Cur, Target: String;
begin
  if CurStep = ssInstall then
  begin
    if LoadStringFromFile(ExpandConstant('{app}\current-version.txt'), Prev) then
      PreviousSha := Trim(String(Prev));
  end
  else if CurStep = ssPostInstall then
  begin
    Cur := ExpandConstant('{app}\current');
    Target := ExpandConstant('{app}\versions\{#Sha12}');
    { rmdir on a junction removes the link, never what it points at. }
    if DirExists(Cur) then
      RunCmd('rmdir "' + Cur + '"');
    if RunCmd('mklink /J "' + Cur + '" "' + Target + '"') <> 0 then
      RaiseException('Could not point ' + Cur + ' at ' + Target);
    SaveStringToFile(ExpandConstant('{app}\current-version.txt'), '{#Sha12}', False);
    PruneVersions;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Cur: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    { Before any file is deleted: drop the junction itself, so nothing walks
      through it into the version it points at. }
    Cur := ExpandConstant('{app}\current');
    if DirExists(Cur) then
      RunCmd('rmdir "' + Cur + '"');
  end;
end;
