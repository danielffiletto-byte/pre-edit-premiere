@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0metadados\Executar.ps1"
if errorlevel 1 if not errorlevel 130 pause
