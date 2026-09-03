@echo off
setlocal
rem Public compatibility wrapper: use the portable PowerShell installer.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\Install-ZGateway.ps1" %*
set EXITCODE=%ERRORLEVEL%
endlocal & exit /b %EXITCODE%
