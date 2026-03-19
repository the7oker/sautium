; Sautium - Inno Setup Installer Script
; Requires: Inno Setup 6.x

[Setup]
AppName=Sautium
AppVersion=0.1.0
AppPublisher=Sautium
DefaultDirName={autopf}\Sautium
DefaultGroupName=Sautium
OutputBaseFilename=Sautium-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
; Don't delete user data on uninstall
UninstallFilesOnly=yes

[Files]
; Launcher executable
Source: "..\..\dist\Sautium.exe"; DestDir: "{app}"; Flags: ignoreversion

; Portable PostgreSQL (pre-downloaded)
Source: "pgsql\*"; DestDir: "{app}\pgsql"; Flags: ignoreversion recursesubdirs

; Embedded Python 3.12 (pre-downloaded, or auto-downloaded on first launch)
Source: "python312\*"; DestDir: "{app}\python312"; Flags: ignoreversion recursesubdirs skipifsourcedoesntexist

; Assets
Source: "..\assets\*"; DestDir: "{app}\desktop\assets"; Flags: ignoreversion

[Icons]
Name: "{group}\Sautium"; Filename: "{app}\Sautium.exe"
Name: "{commondesktop}\Sautium"; Filename: "{app}\Sautium.exe"
Name: "{group}\Uninstall Sautium"; Filename: "{uninstallexe}"

[Run]
; Clone repository on first install
Filename: "git"; Parameters: "clone https://github.com/user/sautium.git ""{app}\repo"""; StatusMsg: "Cloning repository..."; Flags: runhidden waituntilterminated
; Install PyTorch with CUDA support (PyPI default is CPU-only on Windows)
Filename: "{app}\python312\python.exe"; Parameters: "-m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124 --quiet"; StatusMsg: "Installing PyTorch with CUDA..."; Flags: runhidden waituntilterminated
; Install base Python requirements
Filename: "{app}\python312\python.exe"; Parameters: "-m pip install -r ""{app}\repo\backend\requirements-base.txt"" --quiet"; StatusMsg: "Installing dependencies..."; Flags: runhidden waituntilterminated
; Install desktop requirements
Filename: "{app}\python312\python.exe"; Parameters: "-m pip install -r ""{app}\repo\desktop\requirements.txt"" --quiet"; StatusMsg: "Installing launcher dependencies..."; Flags: runhidden waituntilterminated

[UninstallDelete]
; Clean up generated files but NOT %APPDATA%/Sautium
Type: filesandordirs; Name: "{app}\repo"

[Code]
// Check for Git availability
function InitializeSetup(): Boolean;
begin
  if not FileExists(ExpandConstant('{sys}\git.exe')) and
     not FileExists(ExpandConstant('{pf}\Git\cmd\git.exe')) then
  begin
    MsgBox('Git is required but was not found. Please install Git first from https://git-scm.com/', mbError, MB_OK);
    Result := False;
  end else
    Result := True;
end;
