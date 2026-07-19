# API Node Manager - 开发模式启动脚本 (PowerShell)

Write-Host "========================================"
Write-Host "  API Node Manager - 开发模式"
Write-Host "========================================"
Write-Host ""
Write-Host "正在启动开发服务器..."
Write-Host "按 Ctrl+C 可以停止服务器"
Write-Host ""

# 切换到脚本所在目录
Set-Location $PSScriptRoot

# 检查 node_modules 是否存在
if (-not (Test-Path "node_modules")) {
    Write-Host "首次运行，正在安装依赖..."
    npm install
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "[错误] 依赖安装失败" -ForegroundColor Red
        Read-Host "按回车键退出"
        exit 1
    }
}

# 启动开发服务器
npm run dev

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[错误] 启动失败" -ForegroundColor Red
    Read-Host "按回车键退出"
    exit 1
}
