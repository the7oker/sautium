; Sautium — Inno Setup 6 script.
;
; Compiled by desktop/build_windows.py, which stages everything [Files] names
; into build\windows and passes the defines below. From the Inno Setup IDE it
; compiles too, after a `--stage-only` run, with the defaults.
;
; The installer is a carrier: a private CPython, a MinGit and a snapshot of
; the tree. Sautium itself is cloned on first run (bootstrap.py), so a new
; Setup.exe is only ever a new runtime — see build_windows.py.

#ifndef StageDir
  #define StageDir "..\..\build\windows"
#endif
#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

[Setup]
; Stable across versions: it is how Setup finds the previous install to
; upgrade and how Apps & Features lists exactly one Sautium.
AppId={{7F0C3D3E-5B1C-4E9A-9C8E-2D6C5A1F0B77}
AppName=Sautium
AppVersion={#Version}
AppPublisher=Sautium
AppPublisherURL=https://github.com/the7oker/sautium
AppSupportURL=https://github.com/the7oker/sautium
; Per-user, never elevated: the runtime under {app} has to stay writable —
; pip installs the launcher's packages into it — and an unsigned installer
; asking for administrator rights is the worst first impression Windows can
; give. No override to a per-machine install for the same reason.
PrivilegesRequired=lowest
DefaultDirName={userpf}\Sautium
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=Sautium-{#Version}-Setup
SetupIconFile={#StageDir}\Sautium.ico
UninstallDisplayIcon={app}\Sautium.ico
UninstallDisplayName=Sautium
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; The launcher holds this mutex (desktop/utils.py); Setup and Uninstall wait
; for it to go before touching the runtime it runs from.
AppMutex=SautiumLauncher
InfoAfterFile={#StageDir}\First launch.txt

[Files]
Source: "{#StageDir}\runtime\*"; DestDir: "{app}\runtime"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#StageDir}\git\*"; DestDir: "{app}\git"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#StageDir}\payload\*"; DestDir: "{app}\payload"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#StageDir}\bootstrap.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageDir}\Sautium.ico"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; An upgrade lays the three trees down fresh: pip's packages and bytecode
; caches in the old runtime, files an older payload had — all go with them.
; The bootstrap finds its dependency marker gone and reinstalls the
; launcher's packages on the next start.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\git"
Type: filesandordirs; Name: "{app}\payload"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Icons]
; Sautium.exe is the runtime's pythonw under the app's name (build_windows.py).
; The AppUserModelID is the one the launcher sets on its own process
; (desktop/utils.py), so a pinned shortcut and the running window share a
; taskbar button.
Name: "{userprograms}\Sautium"; Filename: "{app}\runtime\Sautium.exe"; Parameters: """{app}\bootstrap.py"""; WorkingDir: "{app}"; IconFilename: "{app}\Sautium.ico"; AppUserModelID: "Sautium.Launcher"
Name: "{userdesktop}\Sautium"; Filename: "{app}\runtime\Sautium.exe"; Parameters: """{app}\bootstrap.py"""; WorkingDir: "{app}"; IconFilename: "{app}\Sautium.ico"; AppUserModelID: "Sautium.Launcher"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\Sautium.exe"; Parameters: """{app}\bootstrap.py"""; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,Sautium}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; pip's packages and __pycache__ are not Setup's files; take the folder whole.
Type: filesandordirs; Name: "{app}"

[Code]
// The uninstaller removes the program. The node — database, settings,
// account key, the app's own clone with the components it downloaded — is
// the user's, so it asks; a silent uninstall keeps it.

procedure StopCluster();
var
  ResultCode: Integer;
  PgCtl, PgData: String;
begin
  // Never delete a data directory under a live postmaster: it keeps running
  // against nothing and holds the port for the next install.
  PgCtl := ExpandConstant('{localappdata}\Sautium\app\pgsql\bin\pg_ctl.exe');
  PgData := ExpandConstant('{localappdata}\Sautium\pgdata');
  if FileExists(PgCtl) and FileExists(PgData + '\PG_VERSION') then
    Exec(PgCtl, '-D "' + PgData + '" -m fast stop', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
end;

procedure RemoveTree(Dir: String);
var
  ResultCode: Integer;
begin
  // rmdir, not DelTree: git marks its pack files read-only and DelTree stops
  // at the first one.
  if DirExists(Dir) then
    Exec(ExpandConstant('{cmd}'), '/c rmdir /s /q "' + Dir + '"', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
end;

procedure RemoveFirewallRules();
var
  ResultCode: Integer;
begin
  // The launcher opened these with an elevation of its own (one UAC prompt
  // per rule); closing them takes one more. Declining leaves the rules and
  // finishes the uninstall.
  ShellExec('runas', 'powershell.exe',
    '-NoProfile -NonInteractive -WindowStyle Hidden -Command "Get-NetFirewallRule -DisplayName ''Sautium (*'' | Remove-NetFirewallRule"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir, ConfigDir, CertDir: String;
begin
  if CurUninstallStep <> usUninstall then
    exit;
  // The elevation prompt would stall a silent uninstall with nobody to answer it.
  if not UninstallSilent then
    RemoveFirewallRules();
  DataDir := ExpandConstant('{localappdata}\Sautium');
  ConfigDir := ExpandConstant('{userappdata}\Sautium');
  CertDir := ExpandConstant('{%USERPROFILE}\.sautium');
  if SuppressibleMsgBox(
       'Also delete the database, settings and account key?' + #13#10#13#10 +
       DataDir + #13#10 + ConfigDir + #13#10 + CertDir + #13#10#13#10 +
       'Choose No to keep them for a later reinstall.',
       mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
  begin
    StopCluster();
    RemoveTree(DataDir);
    RemoveTree(ConfigDir);
    RemoveTree(CertDir);
  end;
end;
