#ifndef AppVersion
  #error Supply AppVersion with the build script.
#endif
[Setup]
AppId={{E62B7D1E-16A7-4B4B-93AA-8AE95AE1E859}
AppName=Aether
AppVersion={#AppVersion}
AppPublisher=Aether
AppPublisherURL=https://github.com/tfassih/aether
AppSupportURL=https://github.com/tfassih/aether/issues
DefaultDirName={localappdata}\Programs\Aether
DefaultGroupName=Aether
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.22000
DisableProgramGroupPage=yes
WizardStyle=modern
Compression=lzma2/fast
SolidCompression=yes
OutputDir=..\dist\release
OutputBaseFilename=Aether-{#AppVersion}-windows-x64-setup
SetupIconFile=..\aether\assets\aether.ico
UninstallDisplayIcon={app}\Aether.exe
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName=Aether
[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked
[Files]
Source: "..\dist\Aether\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Aether"; Filename: "{app}\Aether.exe"
Name: "{userdesktop}\Aether"; Filename: "{app}\Aether.exe"; Tasks: desktopicon
[Run]
Filename: "{app}\Aether.exe"; Description: "Open Aether"; Flags: nowait postinstall skipifsilent
