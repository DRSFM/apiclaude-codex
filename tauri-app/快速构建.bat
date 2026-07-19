@echo off
setlocal
chcp 65001 >nul 2>&1
pushd "%~dp0" || (
  echo Failed to enter app folder.
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File ".\package-exe.ps1" -NoInstaller
set "ERR=%ERRORLEVEL%"
popd
echo.
if not "%ERR%"=="0" (
  echo Failed with exit code %ERR%.
) else (
  echo Done.
)
pause
exit /b %ERR%
