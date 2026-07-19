#!/bin/bash
# API Node Manager - 开发模式启动脚本

echo "========================================"
echo "  API Node Manager - 开发模式"
echo "========================================"
echo ""
echo "正在启动开发服务器..."
echo "按 Ctrl+C 可以停止服务器"
echo ""

# 切换到脚本所在目录
cd "$(dirname "$0")"

# 检查 node_modules 是否存在
if [ ! -d "node_modules" ]; then
    echo "首次运行，正在安装依赖..."
    npm install
    if [ $? -ne 0 ]; then
        echo ""
        echo "[错误] 依赖安装失败"
        exit 1
    fi
fi

# 启动开发服务器
npm run dev

if [ $? -ne 0 ]; then
    echo ""
    echo "[错误] 启动失败"
    exit 1
fi
