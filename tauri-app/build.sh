#!/bin/bash
# API Node Manager - 正式版构建和启动脚本

echo "========================================"
echo "  API Node Manager - 正式版构建"
echo "========================================"
echo ""

# 切换到脚本所在目录
cd "$(dirname "$0")"

# 检查 node_modules 是否存在
if [ ! -d "node_modules" ]; then
    echo "正在安装依赖..."
    npm install
    if [ $? -ne 0 ]; then
        echo ""
        echo "[错误] 依赖安装失败"
        exit 1
    fi
fi

echo "正在构建正式版应用..."
echo "这可能需要几分钟时间，请耐心等待..."
echo ""

npm run build

if [ $? -ne 0 ]; then
    echo ""
    echo "[错误] 构建失败"
    exit 1
fi

echo ""
echo "========================================"
echo "  构建完成！"
echo "========================================"
echo ""

# 检测操作系统
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    echo "可执行文件位置："
    echo "  src-tauri/target/release/api-node-manager"
    echo ""
    echo "应用包位置（如果已生成）："
    echo "  src-tauri/target/release/bundle/macos/API Node Manager.app"
    echo "  src-tauri/target/release/bundle/dmg/"
    echo ""

    if [ -d "src-tauri/target/release/bundle/macos/API Node Manager.app" ]; then
        echo "是否立即运行应用？[y/N]"
        read -r run_app
        if [[ "$run_app" =~ ^[Yy]$ ]]; then
            echo ""
            echo "启动应用..."
            open "src-tauri/target/release/bundle/macos/API Node Manager.app"
            exit 0
        fi
    fi
else
    # Linux
    echo "可执行文件位置："
    echo "  src-tauri/target/release/api-node-manager"
    echo ""
    echo "安装包位置（如果已生成）："
    echo "  src-tauri/target/release/bundle/deb/"
    echo "  src-tauri/target/release/bundle/appimage/"
    echo ""

    if [ -f "src-tauri/target/release/api-node-manager" ]; then
        echo "是否立即运行应用？[y/N]"
        read -r run_app
        if [[ "$run_app" =~ ^[Yy]$ ]]; then
            echo ""
            echo "启动应用..."
            ./src-tauri/target/release/api-node-manager &
            exit 0
        fi
    fi
fi

echo "按任意键退出..."
read -n 1
