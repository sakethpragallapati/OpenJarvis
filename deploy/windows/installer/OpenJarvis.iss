; Jarvis for Windows (OpenJarvis) - setup wizard.
; Built by scripts/windows/build-installer.ps1, which stages the files this
; script packs into build\installer\payload and passes /DAppVersion and
; /DPayload. The wizard copies them, then setup-env.ps1 downloads and builds
; the Python environment (an online installer: PyTorch alone is ~3 GB).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Payload
  #define Payload "..\..\..\build\installer\payload"
#endif
#ifndef RepoUrl
  #define RepoUrl "https://github.com/open-jarvis/openjarvis"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\..\dist"
#endif

[Setup]
AppId={{6F1E7C52-9A4B-4C8E-B1D3-0E5A7C9F2B61}
AppName=Jarvis (OpenJarvis)
AppVersion={#AppVersion}
AppVerName=Jarvis {#AppVersion}
AppPublisher=OpenJarvis contributors
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases
DefaultDirName={localappdata}\OpenJarvis
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=OpenJarvis-Setup-{#AppVersion}
SetupIconFile={#Payload}\jarvis.ico
UninstallDisplayIcon={app}\jarvis.ico
UninstallDisplayName=Jarvis (OpenJarvis)
LicenseFile={#Payload}\LICENSE.txt
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Messages]
WelcomeLabel2=This installs Jarvis, a voice assistant that controls your Windows PC.%n%nSetup downloads about 3-7 GB (Python packages, and the local AI model if you choose it), so keep the PC online. It takes 10-30 minutes.

[Tasks]
Name: "brainlocal"; Description: "On this PC with Ollama + qwen3.5:4b (private, offline; best with an NVIDIA GPU, 6 GB+). Downloads about 3.4 GB."; GroupDescription: "Jarvis's brain (you can change this later in Jarvis's settings):"; Flags: exclusive
Name: "braingroq"; Description: "Groq cloud (fast on any PC; add your free Groq API key in Jarvis's settings)"; GroupDescription: "Jarvis's brain (you can change this later in Jarvis's settings):"; Flags: exclusive unchecked
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start Jarvis (muted, in the tray) when I sign in"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#Payload}\bin\uv.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "{#Payload}\setup\*"; DestDir: "{app}\setup"; Flags: ignoreversion recursesubdirs
Source: "{#Payload}\jarvis.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Payload}\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Payload}\README.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{autoprograms}\Jarvis"; Filename: "{app}\venv\Scripts\jarvis-app.exe"; IconFilename: "{app}\jarvis.ico"; AppUserModelID: "OpenJarvis.JARVIS"; Comment: "Jarvis voice assistant"
Name: "{autoprograms}\Jarvis - repair install"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup\setup-env.ps1"" -App ""{app}"" -Brain repair"; IconFilename: "{app}\jarvis.ico"
Name: "{autodesktop}\Jarvis"; Filename: "{app}\venv\Scripts\jarvis-app.exe"; IconFilename: "{app}\jarvis.ico"; AppUserModelID: "OpenJarvis.JARVIS"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "OpenJarvis"; ValueData: """{app}\venv\Scripts\jarvis-app.exe"" --background"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\venv\Scripts\jarvis-app.exe"; Description: "Start Jarvis now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -Command ""Get-Process | Where-Object {{ $_.Path -like '{app}\*' } | Stop-Process -Force"""; Flags: runhidden; RunOnceId: "StopJarvis"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\venv"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\node"
Type: filesandordirs; Name: "{app}\cache"
Type: filesandordirs; Name: "{app}\setup"
Type: files; Name: "{app}\install.log"

[Code]
var
  EnvFailed: Boolean;

function Brain(): String;
begin
  if WizardIsTaskSelected('braingroq') then
    Result := 'groq'
  else
    Result := 'local';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Params: String;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := 'Downloading and setting up Jarvis (a console window shows the progress)...';
    Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\setup\setup-env.ps1') +
      '" -App "' + ExpandConstant('{app}') + '" -Brain ' + Brain();
    if not Exec('powershell.exe', Params, '', SW_SHOW, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    begin
      EnvFailed := True;
      SuppressibleMsgBox('Jarvis''s files were copied, but setting up its environment failed.' + #13#10 +
        'See ' + ExpandConstant('{app}\install.log') + ', then run "Jarvis - repair install" from the Start menu.',
        mbError, MB_OK, IDOK);
    end;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and not EnvFailed and (Brain() = 'groq') then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      'Jarvis opens its settings on first start: paste your Groq API key (free at console.groq.com/keys).';
end;
