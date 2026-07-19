@echo off
setlocal
chcp 65001 >nul 2>&1
pushd "%~dp0" || (
  echo Failed to enter app folder.
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File ".\check-env.ps1"
set "ERR=%ERRORLEVEL%"
popd
pause
exit /b %ERR%
