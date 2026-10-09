; Windows installer (Inno Setup 6). Build the app first with PyInstaller
; (packaging/LiveTranslate.spec), then:  ISCC packaging\windows\LiveTranslate.iss
; The GitHub Actions workflow does both and attaches the Setup.exe to the release.

#define AppVersion GetEnv("LT_VERSION")
#if AppVersion == ""
  #define AppVersion "1.1.0"
#endif

[Setup]
AppId={{8F2A6C1E-5B7D-4C3A-9E41-2D6B7A9C0F13}
AppName=Live Translate
AppVersion={#AppVersion}
AppVerName=Live Translate {#AppVersion}
AppPublisher=Darbazi
AppPublisherURL=https://github.com/Darbazi-A/live-translate
DefaultDirName={autopf}\Live Translate
DefaultGroupName=Live Translate
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=LiveTranslate-Setup-{#AppVersion}
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\LiveTranslate.exe
UninstallDisplayName=Live Translate
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; admin: needed for the firewall rule that lets phones on the Wi-Fi connect
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\dist\LiveTranslate\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; files of an older version that the new one no longer ships
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\Live Translate"; Filename: "{app}\LiveTranslate.exe"
Name: "{group}\Uninstall Live Translate"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Live Translate"; Filename: "{app}\LiveTranslate.exe"; Tasks: desktopicon

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Live Translate"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Live Translate"" dir=in action=allow program=""{app}\LiveTranslate.exe"" enable=yes profile=any"; Flags: runhidden; StatusMsg: "Allowing phones on the Wi-Fi to connect..."
Filename: "{app}\LiveTranslate.exe"; Description: "Start Live Translate now"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Live Translate"""; Flags: runhidden; RunOnceId: "DelFirewallRule"

[Code]
function ChromeInstalled(): Boolean;
begin
  Result := RegKeyExists(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe') or
            RegKeyExists(HKCU, 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not ChromeInstalled()) and (not WizardSilent()) then
    MsgBox('Google Chrome was not found on this computer.' + #13#10 + #13#10 +
           'The speaker page needs Chrome for speech recognition. Install it from https://www.google.com/chrome' + #13#10 + #13#10 +
           'Listeners can use any browser on their phones.', mbInformation, MB_OK);
end;
