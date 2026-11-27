; K-Trader 운영 설치 스크립트 (Inno Setup 6)
; 빌드: tools/build_installer.py 가 ISCC 로 컴파일
;
; 매크로 (/D 주입): MyAppVersion, StageDir, RuntimeDir, OutputDir

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\..\dist\installer_stage"
#endif
#ifndef RuntimeDir
  #define RuntimeDir "..\..\dist\runtime\python"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

#define MyAppName "K-Trader"
#define MyAppPublisher "K-Trader"
#define MyAppExeName "run_k_trader.bat"

[Setup]
AppId={{8F3C2E17-B4D4-4F9A-9C21-A1B2C3D4E5F6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName=C:\k-trader
DisableDirPage=no
UsePreviousAppDir=yes
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir={#OutputDir}
OutputBaseFilename=K-Trader-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; 앱 소스 — 스테이징에 config/settings/license 없음
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 포터블 Python
Source: "{#RuntimeDir}\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch K-Trader"; Flags: nowait postinstall skipifsilent shellexec

[Code]
procedure InitializeWizard;
begin
  WizardForm.WelcomeLabel2.Caption :=
    'Installs K-Trader and a private Python runtime under C:\k-trader.'#13#10#13#10 +
    'On first launch, an API key / account setup wizard appears.'#13#10 +
    'Existing config.py, settings.json, license.key, and DB files are kept.';
end;
