param(
    [switch]$ForceBuild
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

$exePath = Join-Path $PSScriptRoot "src-tauri\target\release\api-node-manager.exe"
$needBuild = $ForceBuild -or -not (Test-Path -LiteralPath $exePath)

if (-not $needBuild) {
    $exeTime = (Get-Item -LiteralPath $exePath).LastWriteTimeUtc
    $watchPaths = @(
        "src",
        "src-tauri\src",
        "src-tauri\capabilities",
        "src-tauri\Cargo.toml",
        "src-tauri\tauri.conf.json",
        "package.json",
        "package-lock.json"
    )

    foreach ($watchPath in $watchPaths) {
        if (-not (Test-Path -LiteralPath $watchPath)) {
            continue
        }

        $item = Get-Item -LiteralPath $watchPath
        if ($item.PSIsContainer) {
            $newerFile = Get-ChildItem -LiteralPath $watchPath -Recurse -File |
                Where-Object { $_.LastWriteTimeUtc -gt $exeTime } |
                Select-Object -First 1
            if ($newerFile) {
                $needBuild = $true
                break
            }
        } elseif ($item.LastWriteTimeUtc -gt $exeTime) {
            $needBuild = $true
            break
        }
    }
}

if ($needBuild) {
    Write-Host "正在构建源码版 release exe..."
    Invoke-Checked "npm" @("run", "build", "--", "--no-bundle")
}

if (-not (Test-Path -LiteralPath $exePath)) {
    throw "构建完成后仍未找到 exe: $exePath"
}

Write-Host "启动应用: $exePath" -ForegroundColor Green
Start-Process -FilePath $exePath
