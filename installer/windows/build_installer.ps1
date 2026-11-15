#Requires -Version 5.1
<#
  Compila el instalador de Windows: dist\RUFFS-Setup.exe

  1. Exporta una copia limpia de RUFFS (sin datos de usuario) con
     scripts\exportar_limpio.py, a partir del commit indicado (HEAD).
  2. Descarga uv para Windows desde GitHub (astral-sh/uv) y verifica su
     SHA-256 publicado.
  3. Compila installer\windows\RUFFS.iss con Inno Setup 6 (ISCC.exe).

  Uso:
    powershell -ExecutionPolicy Bypass -File installer\windows\build_installer.ps1 [-Ref HEAD]
#>
param(
    [string]$Ref = "HEAD",
    [string]$UvVersion = "0.13.4"
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root

$build = Join-Path $root "build\windows"
$src = Join-Path $build "ruffs"
$dist = Join-Path $root "dist"
New-Item -ItemType Directory -Force $build, $dist | Out-Null

# Python para el exportador: el del entorno del repo o el del sistema.
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = (Get-Command python -ErrorAction Stop).Source }

Write-Host "==> Copia limpia de RUFFS ($Ref)" -ForegroundColor Cyan
& $py scripts\exportar_limpio.py --out $src --ref $Ref
if ($LASTEXITCODE -ne 0) { throw "La exportación limpia falló (ver arriba)." }

Write-Host "==> uv $UvVersion para Windows" -ForegroundColor Cyan
$asset = "uv-x86_64-pc-windows-msvc.zip"
$base = "https://github.com/astral-sh/uv/releases/download/$UvVersion"
$zip = Join-Path $build $asset
Invoke-WebRequest "$base/$asset" -OutFile $zip -UseBasicParsing
$shaFile = "$zip.sha256"
Invoke-WebRequest "$base/$asset.sha256" -OutFile $shaFile -UseBasicParsing
$expected = ((Get-Content $shaFile -Raw).Trim() -split '\s+')[0].ToLower()
$actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected) { throw "SHA-256 de uv no coincide ($actual != $expected)" }
$uvTmp = Join-Path $build "uv"
if (Test-Path $uvTmp) { Remove-Item -Recurse -Force $uvTmp }
Expand-Archive $zip -DestinationPath $uvTmp
New-Item -ItemType Directory -Force (Join-Path $src "tools") | Out-Null
Copy-Item (Get-ChildItem $uvTmp -Recurse -Filter uv.exe | Select-Object -First 1).FullName (Join-Path $src "tools\uv.exe")
# uvx: para los MCP del catálogo que se ejecutan con uvx (fetch, time, git).
Copy-Item (Get-ChildItem $uvTmp -Recurse -Filter uvx.exe | Select-Object -First 1).FullName (Join-Path $src "tools\uvx.exe")

Write-Host "==> Compilando el instalador" -ForegroundColor Cyan
$iscc = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) { throw "No se encontró Inno Setup 6 (winget install JRSoftware.InnoSetup)." }
# Versión visible (archivo VERSION del repositorio, p. ej. 1.1.0); el commit va en RUFFS_BUILD.
$verFile = Join-Path $src "VERSION"
$version = if (Test-Path $verFile) { (Get-Content $verFile -Raw).Trim() } else { "1.0" }
& $iscc "/DSourceDir=$src" "/DAppVersion=$version" "/O$dist" (Join-Path $PSScriptRoot "RUFFS.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC falló." }

$exe = Join-Path $dist "RUFFS-Setup.exe"
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash
Write-Host ""
Write-Host "Listo: $exe" -ForegroundColor Green
Write-Host "SHA-256: $hash"
