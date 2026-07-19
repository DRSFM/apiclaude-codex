param()

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$distExe = Join-Path $PSScriptRoot "dist\API-Node-Manager.exe"
$targetExe = Join-Path $PSScriptRoot "src-tauri\target\release\api-node-manager.exe"

if (Test-Path -LiteralPath $distExe) {
    Start-Process -FilePath $distExe
    exit 0
}

if (Test-Path -LiteralPath $targetExe) {
    Start-Process -FilePath $targetExe
    exit 0
}

Write-Host "未找到已构建的 exe，正在从源码构建..." -ForegroundColor Yellow
& (Join-Path $PSScriptRoot "start-source.ps1") -ForceBuild
exit $LASTEXITCODE
