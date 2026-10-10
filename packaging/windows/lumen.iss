; Inno Setup script for the Windows installer.
; Built by .github/workflows/release.yml after PyInstaller has produced dist\Lumen.
;   iscc /DAppVersion=0.1.0 packaging\windows\lumen.iss
; Installs per user (no admin prompt). Photos and the library are never touched
; by install or uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Lumen"
#define AppExe "Lumen.exe"
; Paths below are relative to this script's folder (packaging\windows).
#define Root "..\.."

[Setup]
AppId={{6E0B8C3A-4D5F-4B7E-9A21-3C8F1E2D7B54}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Gautham Palaniappan
AppPublisherURL=https://github.com/gauthampalaniappan/lumen
AppSupportURL=https://github.com/gauthampalaniappan/lumen/issues
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile={#Root}\LICENSE
SetupIconFile={#Root}\packaging\icons\lumen.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir={#Root}\release
OutputBaseFilename=Lumen-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#Root}\dist\Lumen\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
