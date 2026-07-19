# 诊断前端问题

Write-Host "正在诊断前端问题..."
Write-Host ""

# 检查文件是否存在
Write-Host "[1/4] 检查前端文件..."
$files = @("src\index.html", "src\app.js", "src\style.css")
foreach ($file in $files) {
    if (Test-Path $file) {
        $size = (Get-Item $file).Length
        Write-Host "  ✓ $file ($size bytes)" -ForegroundColor Green
    } else {
        Write-Host "  ✗ $file 不存在!" -ForegroundColor Red
    }
}
Write-Host ""

# 检查 app.js 开头
Write-Host "[2/4] 检查 app.js 导入语句..."
$appJs = Get-Content "src\app.js" -TotalCount 5
Write-Host "  前 5 行:"
$appJs | ForEach-Object { Write-Host "    $_" }
Write-Host ""

# 检查 index.html 脚本标签
Write-Host "[3/4] 检查 index.html 脚本引用..."
$html = Get-Content "src\index.html" -Raw
if ($html -match '<script[^>]*src=[''"]app\.js[''"]') {
    Write-Host "  ✓ 找到 app.js 引用" -ForegroundColor Green
    if ($html -match 'type=[''"]module[''"]') {
        Write-Host "  ✓ 使用 ES 模块模式" -ForegroundColor Green
    } else {
        Write-Host "  ✗ 未使用模块模式" -ForegroundColor Yellow
    }
} else {
    Write-Host "  ✗ 未找到 app.js 引用!" -ForegroundColor Red
}
Write-Host ""

# 检查 package.json
Write-Host "[4/4] 检查 Tauri 依赖..."
$pkg = Get-Content "package.json" | ConvertFrom-Json
Write-Host "  依赖:"
$pkg.dependencies.PSObject.Properties | ForEach-Object {
    Write-Host "    $($_.Name): $($_.Value)"
}
Write-Host ""

Write-Host "========================================"
Write-Host "建议："
Write-Host "1. 运行 dev.ps1 启动应用"
Write-Host "2. 打开开发者工具（F12）"
Write-Host "3. 查看 Console 标签的错误信息"
Write-Host "4. 查看 Network 标签确认文件是否加载"
Write-Host ""

Read-Host "按回车键退出"
