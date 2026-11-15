@echo off
:: RUFFS - doble clic para iniciar. Sin permisos de administrador.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Iniciar_RUFFS.ps1" %*
