# 初始化空配置

$configPath = "$env:USERPROFILE\.apiclaude_config.json"

Write-Host "正在创建 Claude API 配置..."
Write-Host ""

# 检查配置文件是否已存在
if (Test-Path $configPath) {
    Write-Host "配置文件已存在: $configPath" -ForegroundColor Yellow
    $overwrite = Read-Host "是否覆盖? [Y/N]"
    if ($overwrite -ne "Y" -and $overwrite -ne "y") {
        Write-Host "已取消"
        Read-Host "按回车键退出"
        exit 0
    }
}

# API Token 不写入此文件；请通过应用或 apiclaude add 安全录入。
$config = @{
    current = $null
    nodes = @{}
}

# 保存配置
$json = $config | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText($configPath, $json, [System.Text.Encoding]::UTF8)

Write-Host "✓ 配置文件已创建: $configPath" -ForegroundColor Green
Write-Host ""
Write-Host "请通过应用的「添加节点」功能或 apiclaude add 录入 API Token。" -ForegroundColor Yellow
Write-Host "Token 将使用 Windows DPAPI 加密，不会写入此 JSON 文件。"
Write-Host ""
Write-Host "现在可以启动应用查看效果："
Write-Host "  .\dev.ps1"
Write-Host ""

Read-Host "按回车键退出"
