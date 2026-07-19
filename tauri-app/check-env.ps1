# 环境检查脚本 (PowerShell)

Write-Host "========================================"
Write-Host "  环境检查"
Write-Host "========================================"
Write-Host ""

$allGood = $true

# 检查 Node.js
Write-Host "[1/5] 检查 Node.js..."
try {
    $nodeVersion = node --version
    Write-Host "  版本: $nodeVersion" -ForegroundColor Green
    Write-Host "[✓] Node.js 已安装" -ForegroundColor Green
} catch {
    Write-Host "[X] Node.js 未安装" -ForegroundColor Red
    Write-Host "    请访问 https://nodejs.org/ 下载安装" -ForegroundColor Yellow
    $allGood = $false
}
Write-Host ""

# 检查 npm
Write-Host "[2/5] 检查 npm..."
try {
    $npmVersion = npm --version
    Write-Host "  版本: $npmVersion" -ForegroundColor Green
    Write-Host "[✓] npm 已安装" -ForegroundColor Green
} catch {
    Write-Host "[X] npm 未安装" -ForegroundColor Red
    $allGood = $false
}
Write-Host ""

# 检查 Rust
Write-Host "[3/5] 检查 Rust..."
try {
    $cargoVersion = cargo --version
    Write-Host "  版本: $cargoVersion" -ForegroundColor Green
    Write-Host "[✓] Rust 已安装" -ForegroundColor Green
} catch {
    Write-Host "[X] Rust 未安装" -ForegroundColor Red
    Write-Host "    请访问 https://rustup.rs/ 下载安装" -ForegroundColor Yellow
    $allGood = $false
}
Write-Host ""

# 检查项目文件
Write-Host "[4/5] 检查项目文件..."
if (Test-Path "package.json") {
    Write-Host "[✓] package.json 存在" -ForegroundColor Green
} else {
    Write-Host "[X] package.json 未找到" -ForegroundColor Red
    Write-Host "    请确保在 tauri-app 目录下运行此脚本" -ForegroundColor Yellow
    $allGood = $false
}

if (Test-Path "src-tauri") {
    Write-Host "[✓] src-tauri 目录存在" -ForegroundColor Green
} else {
    Write-Host "[X] src-tauri 目录未找到" -ForegroundColor Red
    $allGood = $false
}
Write-Host ""

# 检查依赖
Write-Host "[5/5] 检查依赖..."
if (Test-Path "node_modules") {
    Write-Host "[✓] node_modules 已存在" -ForegroundColor Green
} else {
    Write-Host "[!] node_modules 不存在，需要运行 npm install" -ForegroundColor Yellow
}
Write-Host ""

Write-Host "========================================"
Write-Host "  环境检查完成"
Write-Host "========================================"
Write-Host ""

if ($allGood) {
    Write-Host "✓ 所有检查通过！可以运行:" -ForegroundColor Green
    Write-Host "  - .\dev.ps1 (开发模式)"
    Write-Host "  - .\build.ps1 (构建正式版)"
    Write-Host ""

    if (-not (Test-Path "node_modules")) {
        $install_deps = Read-Host "是否现在安装依赖? [Y/N]"
        if ($install_deps -eq "Y" -or $install_deps -eq "y") {
            Write-Host ""
            Write-Host "正在安装依赖..."
            npm install
            if ($LASTEXITCODE -eq 0) {
                Write-Host ""
                Write-Host "[✓] 依赖安装成功" -ForegroundColor Green
            } else {
                Write-Host ""
                Write-Host "[错误] 依赖安装失败" -ForegroundColor Red
            }
        }
    }
} else {
    Write-Host "X 检查未通过，请先安装缺失的工具" -ForegroundColor Red
}

Write-Host ""
Read-Host "按回车键退出"
