# 测试 Tauri API - 使用测试页面

Write-Host "========================================"
Write-Host "  Tauri API 测试"
Write-Host "========================================"
Write-Host ""
Write-Host "正在准备测试环境..."
Write-Host ""

# 临时修改 index.html 为 test.html
$tauriConf = "src-tauri\tauri.conf.json"
$backup = "src-tauri\tauri.conf.json.backup"

if (Test-Path $tauriConf) {
    Copy-Item $tauriConf $backup
    Write-Host "✓ 已备份配置文件" -ForegroundColor Green
}

# 修改 frontendDist
$content = Get-Content $tauriConf -Raw
$content = $content -replace '"frontendDist": "../src"', '"frontendDist": "../src"'

# 临时移动文件
if (Test-Path "src\index.html") {
    Rename-Item "src\index.html" "index.html.bak"
}
if (Test-Path "src\test.html") {
    Copy-Item "src\test.html" "src\index.html"
}

Write-Host "✓ 已切换到测试页面" -ForegroundColor Green
Write-Host ""
Write-Host "正在启动 Tauri..."
Write-Host "测试页面会自动打开，按照页面上的步骤测试"
Write-Host ""
Write-Host "按 Ctrl+C 停止测试"
Write-Host ""

try {
    npm run dev
} finally {
    # 恢复文件
    if (Test-Path "src\index.html") {
        Remove-Item "src\index.html"
    }
    if (Test-Path "src\index.html.bak") {
        Rename-Item "src\index.html.bak" "index.html"
    }
    if (Test-Path $backup) {
        Move-Item $backup $tauriConf -Force
    }

    Write-Host ""
    Write-Host "已恢复原始文件"
}
