# 初始化示例配置

$configPath = "$env:USERPROFILE\.apiclaude_config.json"

Write-Host "正在创建示例 Claude API 配置..."
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

# 创建示例配置
$config = @{
    current = "示例节点 1"
    nodes = @{
        "示例节点 1" = @{
            base_url = "https://api.anthropic.com/v1"
            token = "sk-ant-api03-示例密钥-请替换为真实密钥"
        }
        "示例节点 2" = @{
            base_url = "https://api.example.com/v1"
            token = "sk-example-key-12345"
        }
    }
}

# 保存配置
$json = $config | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText($configPath, $json, [System.Text.Encoding]::UTF8)

Write-Host "✓ 配置文件已创建: $configPath" -ForegroundColor Green
Write-Host ""
Write-Host "包含 2 个示例节点："
Write-Host "  1. 示例节点 1 (当前节点)"
Write-Host "  2. 示例节点 2"
Write-Host ""
Write-Host "请编辑配置文件，替换为真实的 API 密钥。" -ForegroundColor Yellow
Write-Host ""
Write-Host "现在可以启动应用查看效果："
Write-Host "  .\dev.ps1"
Write-Host ""

Read-Host "按回车键退出"
