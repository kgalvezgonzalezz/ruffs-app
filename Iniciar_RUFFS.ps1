#Requires -Version 5.1
<#
  RUFFS — lanzador para Windows.

  Primera vez: crea el entorno de Python (.venv), instala dependencias y crea
  .env; la cuenta de administrador se crea en el navegador al abrir RUFFS.
  Siguientes veces: arranca directamente; solo reinstala dependencias si
  requirements.txt ha cambiado.

  No necesita permisos de administrador. Por defecto escucha solo en este
  equipo (127.0.0.1). Puerto por defecto: 7100 (Odysseus usaba 7000, así
  que pueden convivir).

  Uso:
    powershell -ExecutionPolicy Bypass -File .\Iniciar_RUFFS.ps1
    powershell -ExecutionPolicy Bypass -File .\Iniciar_RUFFS.ps1 -Port 7200 -OpenBrowser
    powershell -ExecutionPolicy Bypass -File .\Iniciar_RUFFS.ps1 -Window      # ventana propia
    powershell -ExecutionPolicy Bypass -File .\Iniciar_RUFFS.ps1 -SetupOnly   # solo instalar

  -Window abre RUFFS en su propia ventana de escritorio (ruffs_desktop.py).
  Al cerrarla RUFFS sigue encendido (p. ej. para el móvil): vuelve a abrir el
  acceso directo, o pulsa Enter en esta consola, para recuperar la ventana;
  cierra la consola o pulsa Ctrl+C para apagarlo. -SetupOnly prepara Python y las
  dependencias y sale (lo usa el instalador RUFFS-Setup.exe). Si existe
  tools\uv.exe (copia instalada), se usa ese uv y Python se guarda en
  tools\python, así no hace falta tener nada instalado.

  No abre el navegador: entra tú en la dirección que muestra la ventana (o
  usa la pestaña de RUFFS que ya tengas abierta y recárgala). Con
  -OpenBrowser abre una pestaña nueva cuando el servidor está listo.
#>
param(
    [int]$Port = 0,
    [string]$BindHost = "",
    [switch]$OpenBrowser,
    [switch]$Window,
    [switch]$SetupOnly,
    [switch]$NoBrowser   # obsoleto: ya es el comportamiento por defecto
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$Host.UI.RawUI.WindowTitle = "RUFFS"

# Si este script (Windows PowerShell 5.1) se lanza desde PowerShell 7, hereda
# su PSModulePath y no carga sus propios módulos (Get-FileHash,
# Invoke-WebRequest, Select-String… "no se reconoce"). Se quitan esas rutas.
if ($PSVersionTable.PSEdition -eq "Desktop" -and $env:PSModulePath) {
    $wpsModules = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\Modules"
    $paths = @($env:PSModulePath -split ';' | Where-Object { $_ -and $_ -notmatch '\\PowerShell\\7|\\Documents\\PowerShell\\Modules|\\Program Files\\PowerShell\\Modules' })
    if ($paths -notcontains $wpsModules) { $paths += $wpsModules }
    $env:PSModulePath = ($paths -join ';')
}

function Write-Step($msg) { Write-Host ""; Write-Host ("==> " + $msg) -ForegroundColor Cyan }
function Fail($msg) {
    Write-Host ""; Write-Host ("ERROR: " + $msg) -ForegroundColor Red; Write-Host ""
    if ($SetupOnly) {
        Write-Host "Se volverá a intentar al abrir RUFFS. Registro: logs\instalacion.log"
        try { Stop-Transcript | Out-Null } catch {}
        Start-Sleep -Seconds 20; exit 1
    }
    Read-Host "Pulsa Enter para salir"; exit 1
}
# Run a native command without PowerShell turning its stderr (progress bars)
# into terminating errors; returns the exit code.
function Invoke-Native([string]$exe, [string[]]$argv) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    # Out-Host: the output goes to the console, not into the return value.
    # (stderr lines are turned into plain text so they show up cleanly and in the log)
    try { & $exe @argv 2>&1 | ForEach-Object { "$_" } | Out-Host; return $LASTEXITCODE } finally { $ErrorActionPreference = $prev }
}

# Lee APP_BIND / APP_PORT del .env si no se pasaron como parámetro.
function Get-DotEnvValue($name) {
    if (-not (Test-Path ".env")) { return $null }
    foreach ($line in Get-Content ".env" -Encoding UTF8) {
        if ($line -match "^\s*$name\s*=\s*(.*)\s*$") { return $Matches[1].Trim('"', "'", ' ') }
    }
    return $null
}
if (-not $BindHost) { $BindHost = (Get-DotEnvValue "APP_BIND"); if (-not $BindHost) { $BindHost = "127.0.0.1" } }
if ($Port -le 0) { $p = Get-DotEnvValue "APP_PORT"; $Port = if ($p) { [int]$p } else { 7100 } }

if ($SetupOnly) {
    New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot "logs") | Out-Null
    try { Start-Transcript -Path (Join-Path $PSScriptRoot "logs\instalacion.log") -Append | Out-Null } catch {}
}
$env:UV_HTTP_TIMEOUT = "180"   # conexiones lentas: no cortar la descarga de Python

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# Si RUFFS ya está en marcha (cerraste la ventana pero sigue encendido, p. ej.
# para el móvil), -Window solo vuelve a abrir la ventana, sin otro servidor.
if ($Window -and -not $SetupOnly -and (Test-Path $venvPy)) {
    $running = $false
    try { Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/login" -f $Port) -UseBasicParsing -TimeoutSec 2 | Out-Null; $running = $true } catch {}
    if ($running) {
        $env:PYTHONUTF8 = "1"
        & $venvPy ruffs_desktop.py --attach --port $Port
        exit 0
    }
}

# uv: el incluido en la instalación (tools\uv.exe) o el del sistema.
$bundledUv = Join-Path $PSScriptRoot "tools\uv.exe"
if (Test-Path $bundledUv) {
    $uvExe = $bundledUv
    # Python gestionado por uv dentro de la propia instalación.
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $PSScriptRoot "tools\python"
} else {
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    $uvExe = if ($uvCmd) { $uvCmd.Source } else { $null }
}
$reqFiles = @("requirements.txt")
if (Test-Path "requirements-desktop.txt") { $reqFiles += "requirements-desktop.txt" }

# 1. Entorno de Python (3.11 recomendado; acepta 3.11–3.13)
if (-not (Test-Path $venvPy)) {
    Write-Step "Creando el entorno de Python (solo la primera vez)"
    if ($uvExe) {
        for ($try = 1; $try -le 3 -and -not (Test-Path $venvPy); $try++) {
            if ($try -gt 1) { Write-Host "Reintentando ($try/3)…" -ForegroundColor Yellow; Start-Sleep -Seconds 3 }
            if (Test-Path ".venv") { Remove-Item -Recurse -Force ".venv" -ErrorAction SilentlyContinue }
            $pyTarget = "3.11"
            if ($env:UV_PYTHON_INSTALL_DIR) {
                # Python gestionado dentro de la instalación. uv crea además un
                # enlace (junction) "cpython-3.11-…" que Windows bloquea (error
                # 448) cuando el proceso viene del instalador, así que se usa
                # directamente la carpeta real "cpython-3.11.N-…".
                Invoke-Native $uvExe @("python", "install", "3.11", "--no-bin", "--no-registry") | Out-Null
                $real = Get-ChildItem $env:UV_PYTHON_INSTALL_DIR -Directory -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match '^cpython-3\.11\.\d+-' -and (Test-Path (Join-Path $_.FullName "python.exe")) } |
                    Sort-Object { [version]($_.Name -replace '^cpython-(3\.11\.\d+)-.*$', '$1') } -Descending |
                    Select-Object -First 1
                if ($real) { $pyTarget = Join-Path $real.FullName "python.exe" }
            }
            $rc = Invoke-Native $uvExe @("venv", "--python", $pyTarget, ".venv")
            if ($rc -ne 0) { Write-Host "uv venv terminó con código $rc" -ForegroundColor Yellow }
        }
    } else {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if (-not $py) { Fail "No se encontró Python. Instala Python 3.11 (python.org) o uv (https://docs.astral.sh/uv/) y vuelve a ejecutar." }
        $made = $false
        foreach ($v in @("-3.11", "-3.12", "-3.13")) {
            & $py.Source $v -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) { & $py.Source $v -m venv .venv; $made = $true; break }
        }
        if (-not $made) { Fail "Se necesita Python 3.11, 3.12 o 3.13." }
    }
    if (-not (Test-Path $venvPy)) { Fail "No se pudo crear el entorno .venv." }
}

# 2. Dependencias: solo si requirements.txt cambió desde la última instalación
$reqHash = (($reqFiles | ForEach-Object { (Get-FileHash $_ -Algorithm SHA256).Hash }) -join "")
$stamp = Join-Path $PSScriptRoot ".venv\ruffs_requirements.sha256"
$installed = if (Test-Path $stamp) { (Get-Content $stamp -Raw).Trim() } else { "" }
if ($installed -ne $reqHash) {
    Write-Step "Instalando dependencias (la primera vez tarda unos minutos)"
    $reqArgs = @(); foreach ($r in $reqFiles) { $reqArgs += @("-r", $r) }
    $rc = 1
    for ($try = 1; $try -le 3 -and $rc -ne 0; $try++) {
        if ($try -gt 1) { Write-Host "Reintentando la instalación ($try/3)…" -ForegroundColor Yellow; Start-Sleep -Seconds 3 }
        if ($uvExe) { $rc = Invoke-Native $uvExe (@("pip", "install", "--python", $venvPy) + $reqArgs) }
        else { Invoke-Native $venvPy @("-m", "pip", "install", "--upgrade", "pip", "--quiet") | Out-Null; $rc = Invoke-Native $venvPy (@("-m", "pip", "install") + $reqArgs) }
    }
    if ($rc -ne 0) { Fail "Falló la instalación de dependencias. Revisa el error de arriba." }
    Set-Content -Path $stamp -Value $reqHash -Encoding ASCII
}

# 3. Configuración inicial. La base de datos y las carpetas las crea la app al
#    arrancar, y la cuenta de administrador se crea en la propia página de
#    acceso la primera vez (así funciona también con la ventana minimizada).
if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Creado .env a partir de .env.example (pon ahí tu OPENROUTER_API_KEY)." -ForegroundColor Yellow
    if (Test-Path $bundledUv) {
        # Copia instalada: escuchar en todas las interfaces (0.0.0.0) para
        # poder entrar desde otros dispositivos con Tailscale. La primera
        # cuenta de administrador solo se puede crear desde este equipo.
        $lines = Get-Content ".env" -Encoding UTF8 | ForEach-Object { $_ -replace '^APP_BIND=.*$', 'APP_BIND=0.0.0.0' }
        [IO.File]::WriteAllLines((Join-Path $PSScriptRoot ".env"), [string[]]$lines, (New-Object Text.UTF8Encoding $false))
        $BindHost = "0.0.0.0"
    }
}

# Actualizaciones: ajustes nuevos en un .env ya existente (el instalador lo
# conserva). 1.1.3: el límite de rondas del agente pasa de 60 a 200 y aparece el
# tope de gasto por mensaje. Solo se toca el valor antiguo por defecto.
if (Test-Path ".env") {
    $envPath = Join-Path $PSScriptRoot ".env"
    $envText = [IO.File]::ReadAllText($envPath)
    $envNew = $envText -replace '(?m)^RUFFS_AGENT_MAX_ROUNDS=60(\r?)$', 'RUFFS_AGENT_MAX_ROUNDS=200$1'
    if ($envNew -notmatch '(?m)^RUFFS_AGENT_MAX_COST_USD=') {
        $envNew = $envNew.TrimEnd() + "`r`n# Tope de gasto por mensaje en dólares (coste real de OpenRouter); 0 = sin tope.`r`nRUFFS_AGENT_MAX_COST_USD=1.0`r`n"
    }
    if ($envNew -ne $envText) {
        [IO.File]::WriteAllText($envPath, $envNew, (New-Object Text.UTF8Encoding $false))
        Write-Host "Ajustes del agente actualizados en .env (200 rondas, tope de 1 USD por mensaje)."
    }
}
New-Item -ItemType Directory -Force "data" | Out-Null

# 3b. Node.js portátil (copia instalada): lo usan el MCP de navegador y los
#     MCP del catálogo que van con npx. Queda en tools\node, sin tocar el
#     sistema. Si falla (sin conexión), RUFFS funciona igual sin esos MCP.
if (Test-Path $bundledUv) {
    $NodeVersion = "v26.13.1"
    $PlaywrightMcp = "@playwright/mcp@0.0.85"   # igual que src\builtin_mcp.py
    # MCP del catálogo que van con npx (igual que src\mcp_catalog.py): se
    # descargan ya para que activarlos en Ajustes → MCP sea inmediato.
    $CatalogMcps = @("rea-agents@6.9.0", "agent-desktop@1.3.1", "mcp-remote@0.16.0")
    $toolsDir = Join-Path $PSScriptRoot "tools"
    $nodeDir = Join-Path $toolsDir "node"
    if (-not (Test-Path (Join-Path $nodeDir "node.exe"))) {
        Write-Step "Instalando Node.js $NodeVersion para los MCP (solo la primera vez)"
        $prevEap = $ErrorActionPreference; $ErrorActionPreference = "Stop"
        try {
            $name = "node-$NodeVersion-win-x64"
            $base = "https://nodejs.org/dist/$NodeVersion"
            $tmp = Join-Path $toolsDir ".node-download"
            if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
            New-Item -ItemType Directory -Force $tmp | Out-Null
            $zip = Join-Path $tmp "$name.zip"
            Invoke-WebRequest "$base/$name.zip" -OutFile $zip -UseBasicParsing
            Invoke-WebRequest "$base/SHASUMS256.txt" -OutFile (Join-Path $tmp "SHASUMS256.txt") -UseBasicParsing
            $line = Select-String -Path (Join-Path $tmp "SHASUMS256.txt") -Pattern ("  " + [regex]::Escape("$name.zip") + "$") | Select-Object -First 1
            $expected = ($line.Line -split '\s+')[0].ToLower()
            $actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
            if (-not $expected -or $actual -ne $expected) { throw "SHA-256 de Node.js no coincide" }
            & "$env:SystemRoot\System32\tar.exe" -xf $zip -C $tmp
            if ($LASTEXITCODE -ne 0) { throw "No se pudo descomprimir Node.js" }
            if (Test-Path $nodeDir) { Remove-Item -Recurse -Force $nodeDir }
            Move-Item (Join-Path $tmp $name) $nodeDir
            Remove-Item -Recurse -Force $tmp
            Write-Host "Node.js $NodeVersion listo."
        } catch {
            Write-Host ("No se pudo instalar Node.js ahora (" + $_.Exception.Message + "). Los MCP con npx no estarán disponibles hasta el próximo arranque.") -ForegroundColor Yellow
        } finally { $ErrorActionPreference = $prevEap }
    }
    # Node y uv/uvx de la instalación primero en el PATH (RUFFS y sus MCP).
    $env:PATH = "$nodeDir;$toolsDir;$env:PATH"
    $npmCache = Join-Path $nodeDir ".ruffs-playwright-mcp"
    if ((Test-Path (Join-Path $nodeDir "npx.cmd")) -and -not (Test-Path $npmCache)) {
        Write-Step "Preparando el MCP de navegador (solo la primera vez)"
        $rc = Invoke-Native (Join-Path $nodeDir "npx.cmd") @("-y", $PlaywrightMcp, "--version")
        if ($rc -eq 0) { Set-Content -Path $npmCache -Value $PlaywrightMcp -Encoding ASCII }
        else { Write-Host "No se pudo preparar ahora; se reintentará al arrancar." -ForegroundColor Yellow }
    }
    $catalogStamp = Join-Path $nodeDir ".ruffs-catalog-mcp"
    $catalogWant = $CatalogMcps -join " "
    $catalogHave = if (Test-Path $catalogStamp) { (Get-Content $catalogStamp -Raw).Trim() } else { "" }
    if ((Test-Path (Join-Path $nodeDir "npx.cmd")) -and $catalogHave -ne $catalogWant) {
        Write-Step "Preparando los MCP REA, Agent Desktop y HyperFrames (solo la primera vez)"
        $allOk = $true
        foreach ($pkg in $CatalogMcps) {
            # Misma caché que "npx -y <paquete> ..." (la que usará RUFFS al activarlo).
            $rc = Invoke-Native (Join-Path $nodeDir "npx.cmd") @("-y", "-p", $pkg, "node", "-e", "0")
            if ($rc -ne 0) { $allOk = $false; Write-Host "  $pkg no se pudo preparar ahora; se descargará al activarlo." -ForegroundColor Yellow }
        }
        if ($allOk) { Set-Content -Path $catalogStamp -Value $catalogWant -Encoding ASCII }
    }
}

if ($SetupOnly) {
    # Descarga ya el modelo de embeddings (memoria/RAG, ~90 MB) para que la
    # primera apertura de RUFFS no tenga que esperar por él. Si falla (sin
    # conexión), RUFFS lo descargará al arrancar.
    Write-Step "Descargando el modelo de memoria (una sola vez)"
    $env:PYTHONUTF8 = "1"
    $rc = Invoke-Native $venvPy @("-c", "import os; from fastembed import TextEmbedding; d=os.path.join('data','fastembed_cache'); os.makedirs(d, exist_ok=True); TextEmbedding(model_name='sentence-transformers/all-MiniLM-L6-v2', cache_dir=d); print('Modelo listo')")
    if ($rc -ne 0) { Write-Host "No se pudo descargar ahora; se hará al abrir RUFFS." -ForegroundColor Yellow }
    Write-Step "RUFFS está instalado y listo"
    Write-Host "Ábrelo desde el menú Inicio o el acceso directo RUFFS."
    try { Stop-Transcript | Out-Null } catch {}
    exit 0
}

# 4. Arranque
$env:PYTHONUTF8 = "1"
$env:NUMBA_DISABLE_JIT = "1"   # evita el error "opcode 34" de numba en Windows
$url = "http://{0}:{1}" -f ($(if ($BindHost -eq "0.0.0.0") { "127.0.0.1" } else { $BindHost }), $Port)
Write-Step ("Iniciando RUFFS en " + $url)
if ($BindHost -eq "0.0.0.0") {
    Write-Host "RUFFS acepta conexiones desde otros dispositivos (Tailscale / tu red), con inicio de sesión." -ForegroundColor Yellow
    $ts = Get-Command tailscale -ErrorAction SilentlyContinue
    if (-not $ts -and (Test-Path "$env:ProgramFiles\Tailscale\tailscale.exe")) { $ts = Get-Item "$env:ProgramFiles\Tailscale\tailscale.exe" }
    if ($ts) {
        $tsPath = if ($ts.Source) { $ts.Source } else { $ts.FullName }
        $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        $tsIp = (& $tsPath ip -4 2>$null | Select-Object -First 1)
        $ErrorActionPreference = $prev
        if ($tsIp) { Write-Host ("Desde tus otros dispositivos con Tailscale: http://{0}:{1}" -f $tsIp.Trim(), $Port) -ForegroundColor Green }
    }
}
Write-Host ("Abre " + $url + " en tu navegador (o recarga la pestaña de RUFFS que ya tengas).")
Write-Host "Cierra esta ventana o pulsa Ctrl+C para detenerlo."

if ($OpenBrowser -and -not $NoBrowser) {
    # Abre el navegador cuando el servidor responda (máx. ~60 s).
    Start-Job -ScriptBlock {
        param($u)
        for ($i = 0; $i -lt 60; $i++) {
            try { Invoke-WebRequest -Uri "$u/login" -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process $u; return } catch { Start-Sleep 1 }
        }
    } -ArgumentList $url | Out-Null
}

if ($Window) {
    Write-Host "RUFFS se abrirá en su propia ventana. Cerrarla no lo apaga: cierra esta consola o pulsa Ctrl+C."
    & $venvPy ruffs_desktop.py --host $BindHost --port $Port
} else {
    & $venvPy -m uvicorn app:app --host $BindHost --port $Port
}
