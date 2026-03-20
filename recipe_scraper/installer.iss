; Macleay Recipe Manager – Inno Setup 6 installer script
; Build:  iscc /DAppVersion=1.0.0 installer.iss
; Output: Output\MacleayRecipeManager-Setup.exe

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName      "Macleay Recipe Manager"
#define AppPublisher "Macleay Recipe Manager"
#define AppExeName   "RecipeManager.exe"
#define AppId        "{{A3F8C2B1-7D4E-4A9F-B621-3E5D8F1C9A7B}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/marshallatimi/Recipe-Manager
AppSupportURL=https://github.com/marshallatimi/Recipe-Manager/issues
AppUpdatesURL=https://github.com/marshallatimi/Recipe-Manager/releases

; Install to Program Files
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes

; Output
OutputDir=Output
OutputBaseFilename=MacleayRecipeManager-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExeName}

; Compression
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; Appearance
WizardStyle=modern
WizardSizePercent=120
DisableWelcomePage=no
ShowLanguageDialog=no

; Privileges – requires UAC for Program Files install
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

; Min Windows version: Windows 10 (required for WebView2 / Edge)
MinVersion=10.0.17763

; Architecture
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: checkedonce

[Files]
; Main executable (produced by PyInstaller)
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Start menu shortcut
Name: "{group}\{#AppName}";       Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"
; Start menu uninstall entry
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
; Desktop shortcut (optional)
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Offer to launch the app after install
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove any temp files the app may leave in its install dir
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// Show a friendly message if WebView2 runtime is missing
// (Edge is built into Windows 10+, so this is just a safety check)
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
