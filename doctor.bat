@echo off
setlocal
set "SPA_REPO_ROOT=%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SPA_REPO_ROOT%tools\doctor.ps1" %*
set "SPA_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %SPA_EXIT_CODE%
