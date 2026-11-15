; RUFFS — instalador para Windows (Inno Setup 6).
; Se compila con installer\windows\build_installer.ps1, que prepara:
;   SourceDir  = copia limpia de RUFFS (scripts\exportar_limpio.py) + tools\uv.exe
;   AppVersion = versión (commit)
;
; Instala sin permisos de administrador en %LOCALAPPDATA%\Programs\RUFFS.
; Al terminar descarga Python 3.11 y las dependencias con el uv incluido
; (no hace falta tener Python). Los datos del usuario (data\, .env) se
; crean en la primera ejecución y se conservan al actualizar.

#ifndef SourceDir
  #error "Falta /DSourceDir=<copia limpia>"
#endif
#ifndef AppVersion
  #define AppVersion "1.0"
#endif

#define AppName "RUFFS"
#define PSExe "{sys}\WindowsPowerShell\v1.0\powershell.exe"
; Comillas dobles duplicadas: dentro de Parameters: "..." Inno las convierte en una.
#define LaunchArgs '-NoProfile -ExecutionPolicy Bypass -File ""{app}\Iniciar_RUFFS.ps1""'

[Setup]
AppId={{6E7C2E4B-3F8A-4C55-9C1E-7A2F0B5D9E31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=RUFFS
DefaultDirName={localappdata}\Programs\RUFFS
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; RedirectionGuard (activo por defecto desde Inno Setup 6.5) se hereda en los
; procesos que lanza el instalador y bloquea las junctions que crea uv al
; instalar Python (error 448). Protege contra escaladas de privilegios en
; instalaciones elevadas; este instalador corre sin privilegios, así que no
; aporta nada aquí.
RedirectionGuard=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputBaseFilename=RUFFS-Setup
SetupIconFile={#SourceDir}\ruffs.ico
UninstallDisplayIcon={app}\ruffs.ico
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ShowLanguageDialog=auto
CloseApplications=yes

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
es.SetupDeps=Instalando Python y las dependencias de RUFFS (la primera vez tarda unos minutos)...
en.SetupDeps=Installing Python and RUFFS dependencies (takes a few minutes the first time)...
es.SetupDepsFailed=No se pudieron instalar todas las dependencias ahora (¿sin conexión?). RUFFS lo volverá a intentar automáticamente la primera vez que lo abras. Detalles en la carpeta de RUFFS: logs\instalacion.log
en.SetupDepsFailed=Some dependencies could not be installed now (offline?). RUFFS will retry automatically the first time you open it. Details in the RUFFS folder: logs\instalacion.log
es.OpenRuffs=Abrir RUFFS
en.OpenRuffs=Open RUFFS
es.FirewallTask=Permitir el acceso desde tus dispositivos de Tailscale (puerto 7100; Windows pedirá permiso de administrador una vez)
en.FirewallTask=Allow access from your Tailscale devices (port 7100; Windows asks for administrator permission once)
es.RemoteGroup=Acceso remoto:
en.RemoteGroup=Remote access:

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "tailscalefw"; Description: "{cm:FirewallTask}"; GroupDescription: "{cm:RemoteGroup}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Dirs]
Name: "{app}\data"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{#PSExe}"; Parameters: "{#LaunchArgs} -Window"; WorkingDir: "{app}"; IconFilename: "{app}\ruffs.ico"; Comment: "RUFFS — tu agente compañero"
Name: "{autoprograms}\{#AppName} (navegador)"; Filename: "{#PSExe}"; Parameters: "{#LaunchArgs} -OpenBrowser"; WorkingDir: "{app}"; IconFilename: "{app}\ruffs.ico"; Comment: "Abre RUFFS en el navegador"
Name: "{autodesktop}\{#AppName}"; Filename: "{#PSExe}"; Parameters: "{#LaunchArgs} -Window"; WorkingDir: "{app}"; IconFilename: "{app}\ruffs.ico"; Tasks: desktopicon

[Run]
Filename: "{#PSExe}"; Parameters: "{#LaunchArgs} -Window"; WorkingDir: "{app}"; Description: "{cm:OpenRuffs}"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; Quita la regla de firewall de RUFFS (si se creó). Pide permiso de administrador.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""RUFFS (Tailscale)"""; Verb: "runas"; Flags: shellexec runhidden waituntilterminated; RunOnceId: "RuffsFirewall"; Tasks: tailscalefw

[Code]
// Python que usará RUFFS (tools\python\cpython-3.11.N-…\python.exe), para que
// la regla de firewall sea de ese programa: así Windows no muestra además su
// propio aviso de firewall al arrancar RUFFS.
function RuffsPythonExe(): String;
var
  FindRec: TFindRec;
  Base: String;
begin
  Result := '';
  Base := ExpandConstant('{app}\tools\python\');
  if FindFirst(Base + 'cpython-3.11.*', FindRec) then
  begin
    try
      repeat
        if FileExists(Base + FindRec.Name + '\python.exe') then
          Result := Base + FindRec.Name + '\python.exe';
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

// Regla de firewall: solo el rango de Tailscale (100.64.0.0/10) puede entrar
// al puerto 7100 de RUFFS. Tu red local (wifi) sigue bloqueada. Una sola
// petición de permiso de administrador (UAC) para borrar la anterior y crearla.
procedure AddTailscaleFirewallRule();
var
  ResultCode: Integer;
  Py, Rule, Cmd: String;
begin
  Py := RuffsPythonExe();
  Rule := 'name="RUFFS (Tailscale)" dir=in action=allow protocol=TCP localport=7100 remoteip=100.64.0.0/10 profile=any';
  if Py <> '' then
    Rule := Rule + ' program="' + Py + '"';
  Cmd := '/c netsh advfirewall firewall delete rule name="RUFFS (Tailscale)" >nul 2>&1 & ' +
         'netsh advfirewall firewall add rule ' + Rule;
  if not ShellExec('runas', ExpandConstant('{cmd}'), Cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Log('No se pudo crear la regla de firewall (permiso denegado o cancelado).');
end;

// Prepara Python + dependencias en una consola visible (se ve el progreso) y
// avisa si falla: RUFFS lo reintentará al abrirse.
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Params: String;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := CustomMessage('SetupDeps');
    Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}') + '\Iniciar_RUFFS.ps1" -SetupOnly';
    if not Exec(ExpandConstant('{#PSExe}'), Params, ExpandConstant('{app}'), SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      SuppressibleMsgBox(CustomMessage('SetupDepsFailed'), mbInformation, MB_OK, IDOK);
    if WizardIsTaskSelected('tailscalefw') then
      AddTailscaleFirewallRule();
  end;
end;

[UninstallDelete]
; Lo regenerable se borra; data\ y .env (tus datos) se conservan.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\tools\python"
Type: filesandordirs; Name: "{app}\tools\node"
Type: filesandordirs; Name: "{app}\__pycache__"
