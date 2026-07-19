@echo off
setlocal
chcp 65001 >nul 2>&1
pushd "%~dp0" || (
  echo Failed to enter app folder.
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File ".\start-source.ps1" -ForceBuild
set "ERR=%ERRORLEVEL%"
popd
if not "%ERR%"=="0" (
  echo.
  echo Failed with exit code %ERR%.
  pause
)
exit /b %ERR%
