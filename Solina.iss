; =============================================================================
; Solina - Inno Setup script (builds Solina_Setup.exe, the installation wizard)
; Compile with: Inno Setup 6.3+  ->  open this file and press Build > Compile,
; or just run build.bat (it calls ISCC.exe for you).
; Needs the folder dist\Solina\ produced by PyInstaller (build.bat makes it).
; =============================================================================
#define MyAppName      "Solina"
#define MyAppVersion   "1.0.0"
#define MyAppPublisher "Tarun"
#define MyAppExe       "Solina.exe"

[Setup]
AppId={{51C20EF1-07DF-4BF3-973F-B0FC55E4761D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Per-user install (no admin prompt). Solina writes solina_log.txt next to the
; exe, which would NOT be writable inside C:\Program Files, so we avoid that.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 64-bit only
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer_output
OutputBaseFilename=Solina_Setup_{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExe}
#if FileExists("solina.ico")
SetupIconFile=solina.ico
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\Solina\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove the log file the app creates after installation
Type: files; Name: "{app}\solina_log.txt"
