param(
    [switch]$Installer,
    [switch]$NoInstaller,
    [switch]$RunAfterBuild
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Assert-Command {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "缺少命令: $Name。请先安装 Node.js/Rust/Tauri 所需环境。"
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$File,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令失败: $File $($Arguments -join ' ')"
    }
}

Assert-Command "npm"
Assert-Command "cargo"

if (-not (Test-Path -LiteralPath "node_modules")) {
    Write-Host "首次运行，正在安装前端依赖..."
    Invoke-Checked "npm" @("install")
}

$installerBuilt = $false

if ($NoInstaller -or -not $Installer) {
    Write-Host "正在构建 release exe（不生成安装包）..."
    Invoke-Checked "npm" @("run", "build", "--", "--no-bundle")
} else {
    Write-Host "正在构建 release exe 和安装包..."
    try {
        Invoke-Checked "npm" @("run", "build")
        $installerBuilt = $true
    } catch {
        Write-Host ""
        Write-Host "安装包构建失败，通常是 NSIS 下载超时。将改为只生成可运行 exe。" -ForegroundColor Yellow
        Write-Host $_.Exception.Message -ForegroundColor DarkYellow
        Write-Host ""
        Invoke-Checked "npm" @("run", "build", "--", "--no-bundle")
    }
}

$targetExe = Join-Path $PSScriptRoot "src-tauri\target\release\api-node-manager.exe"
if (-not (Test-Path -LiteralPath $targetExe)) {
    throw "未找到构建产物: $targetExe"
}

$distDir = Join-Path $PSScriptRoot "dist"
New-Item -ItemType Directory -Path $distDir -Force | Out-Null

$distExe = Join-Path $distDir "API-Node-Manager.exe"
Copy-Item -LiteralPath $targetExe -Destination $distExe -Force

Write-Host ""
Write-Host "已生成可直接运行的 exe:" -ForegroundColor Green
Write-Host "  $distExe"

if ($installerBuilt) {
    $nsisDir = Join-Path $PSScriptRoot "src-tauri\target\release\bundle\nsis"
    if (Test-Path -LiteralPath $nsisDir) {
        $setup = Get-ChildItem -LiteralPath $nsisDir -Filter "*.exe" -File |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
        if ($setup) {
            $setupDest = Join-Path $distDir $setup.Name
            Copy-Item -LiteralPath $setup.FullName -Destination $setupDest -Force
            Write-Host ""
            Write-Host "已生成安装包:" -ForegroundColor Green
            Write-Host "  $setupDest"
        }
    }
}

if ($RunAfterBuild) {
    Start-Process -FilePath $distExe
}
