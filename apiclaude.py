#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude API 节点管理工具
管理多个 Claude API 站点的配置
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG_FILE = Path.home() / ".apiclaude_config.json"


def load_config():
    """加载配置文件"""
    if not CONFIG_FILE.exists():
        return {"nodes": {}, "current": None}

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"错误：无法读取配置文件: {e}")
        return {"nodes": {}, "current": None}


def save_config(config):
    """保存配置文件"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"错误：无法保存配置文件: {e}")
        return False


def mask_token(token):
    """返回脱敏后的 token"""
    return f"{token[:20]}..." if len(token) > 20 else token


def generate_env_scripts(name, node):
    """生成当前节点的环境变量脚本"""
    if os.name == 'nt':  # Windows
        ps_script = Path.home() / ".apiclaude_env.ps1"
        with open(ps_script, 'w', encoding='utf-8') as f:
            f.write(f'$env:ANTHROPIC_BASE_URL="{node["base_url"]}"\n')
            f.write(f'$env:ANTHROPIC_AUTH_TOKEN="{node["token"]}"\n')
            f.write('Write-Host "已加载 API 节点: ' + name + '" -ForegroundColor Green\n')

        cmd_script = Path.home() / ".apiclaude_env.bat"
        with open(cmd_script, 'w', encoding='utf-8') as f:
            f.write('@echo off\n')
            f.write(f'set "ANTHROPIC_BASE_URL={node["base_url"]}"\n')
            f.write(f'set "ANTHROPIC_AUTH_TOKEN={node["token"]}"\n')
            f.write(f'echo 已加载 API 节点: {name}\n')
    else:  # Linux/Mac
        sh_script = Path.home() / ".apiclaude_env.sh"
        with open(sh_script, 'w', encoding='utf-8') as f:
            f.write(f'export ANTHROPIC_BASE_URL="{node["base_url"]}"\n')
            f.write(f'export ANTHROPIC_AUTH_TOKEN="{node["token"]}"\n')
            f.write(f'echo "已加载 API 节点: {name}"\n')


def list_nodes(config):
    """列出所有节点"""
    if not config["nodes"]:
        print("还没有添加任何节点。使用 'apiclaude add' 来添加节点。")
        return

    print("\n可用的 API 节点：")
    print("-" * 60)

    current = config.get("current")
    for i, (name, node) in enumerate(config["nodes"].items(), 1):
        marker = " [当前]" if name == current else ""
        print(f"{i}. {name}{marker}")
        print(f"   Base URL: {node['base_url']}")
        print(f"   Token: {mask_token(node['token'])}")
        print()


def add_node(config):
    """添加新节点"""
    print("\n添加新的 API 节点")
    print("-" * 60)

    name = input("节点名称: ").strip()
    if not name:
        print("错误：节点名称不能为空")
        return

    if name in config["nodes"]:
        confirm = input(f"节点 '{name}' 已存在，是否覆盖？(y/N): ").strip().lower()
        if confirm != 'y':
            print("取消操作")
            return

    base_url = input("ANTHROPIC_BASE_URL: ").strip()
    if not base_url:
        print("错误：Base URL 不能为空")
        return

    token = input("ANTHROPIC_AUTH_TOKEN: ").strip()
    if not token:
        print("错误：Token 不能为空")
        return

    config["nodes"][name] = {
        "base_url": base_url,
        "token": token
    }

    if save_config(config):
        print(f"\n成功添加节点 '{name}'")

        # 如果这是第一个节点，自动设为当前节点
        if not config.get("current"):
            config["current"] = name
            save_config(config)
            node = config["nodes"][name]
            apply_node(name, node)
            print(f"已自动设置 '{name}' 为当前节点")
            generate_env_scripts(name, node)


def remove_node(config, name):
    """删除节点"""
    if name not in config["nodes"]:
        print(f"错误：节点 '{name}' 不存在")
        return

    confirm = input(f"确定要删除节点 '{name}'？(y/N): ").strip().lower()
    if confirm != 'y':
        print("取消操作")
        return

    del config["nodes"][name]

    # 如果删除的是当前节点，清除当前节点标记
    if config.get("current") == name:
        config["current"] = None

    if save_config(config):
        print(f"成功删除节点 '{name}'")


def apply_node(name, node):
    """应用节点配置到环境变量"""
    os.environ["ANTHROPIC_BASE_URL"] = node["base_url"]
    os.environ["ANTHROPIC_AUTH_TOKEN"] = node["token"]

    print(f"\n已设置环境变量：")
    print(f"ANTHROPIC_BASE_URL={node['base_url']}")
    print(f"ANTHROPIC_AUTH_TOKEN={mask_token(node['token'])}")


def launch_claude(claude_args=None):
    """在当前环境变量下启动 Claude Code"""
    claude = shutil.which("claude")
    if not claude:
        print("\n错误：找不到 claude 命令，请确认 Claude Code 已安装并在 PATH 中。")
        return 1

    print("\n正在启动 Claude Code...\n")
    sys.stdout.flush()
    return subprocess.call([claude] + (claude_args or []))


def use_node(config, name, launch=False, claude_args=None):
    """设置当前节点，并可选择直接启动 Claude Code"""
    if name not in config["nodes"]:
        print(f"错误：节点 '{name}' 不存在")
        return 1

    node = config["nodes"][name]
    config["current"] = name
    save_config(config)
    apply_node(name, node)
    generate_env_scripts(name, node)

    print(f"\n已切换到节点 '{name}'")

    if launch:
        return launch_claude(claude_args)
    return 0


def use_current_node(config, launch=False, claude_args=None):
    """使用当前节点，并可选择直接启动 Claude Code"""
    current = config.get("current")
    if not current:
        print("当前没有选择节点。使用 'apiclaude select' 选择节点，或 'apiclaude add' 添加节点。")
        return 1

    if current not in config["nodes"]:
        print(f"错误：当前节点 '{current}' 不存在")
        config["current"] = None
        save_config(config)
        return 1

    return use_node(config, current, launch=launch, claude_args=claude_args)


def select_node(config, launch=False, claude_args=None):
    """选择节点"""
    if not config["nodes"]:
        print("还没有添加任何节点。使用 'apiclaude add' 来添加节点。")
        return 1

    list_nodes(config)

    choice = input("请选择节点编号（直接回车取消）: ").strip()
    if not choice:
        return 1

    try:
        choice = int(choice)
    except ValueError:
        print("错误：无效的输入")
        return 1

    if choice < 1 or choice > len(config["nodes"]):
        print("错误：无效的选择")
        return 1

    name = list(config["nodes"].keys())[choice - 1]
    return use_node(config, name, launch=launch, claude_args=claude_args)


def show_current(config):
    """显示当前节点"""
    current = config.get("current")
    if not current:
        print("当前没有选择节点")
        return

    if current not in config["nodes"]:
        print(f"错误：当前节点 '{current}' 不存在")
        config["current"] = None
        save_config(config)
        return

    node = config["nodes"][current]
    print(f"\n当前节点: {current}")
    print(f"ANTHROPIC_BASE_URL={node['base_url']}")
    print(f"ANTHROPIC_AUTH_TOKEN={mask_token(node['token'])}")


def print_usage():
    """打印使用说明"""
    print("""
Claude API 节点管理工具

用法:
  apiclaude              选择节点并启动 Claude Code
  apiclaude [CLAUDE_ARGS] 选择节点并启动 Claude Code，并传递参数
  apiclaude select       选择节点并启动 Claude Code
  apiclaude run [ARGS]   使用当前节点启动 Claude Code，并传递参数
  apiclaude list         列出所有节点
  apiclaude add          添加新节点
  apiclaude remove NAME  删除指定节点
  apiclaude current      显示当前节点
  apiclaude help         显示此帮助信息

示例:
  apiclaude              # 交互式选择节点后进入 Claude Code
  apiclaude --permission-mode bypassPermissions
  apiclaude resume       # 选择节点后执行 Claude Code resume
  apiclaude -c           # 选择节点后执行 Claude Code -c
  apiclaude select       # 交互式选择节点后进入 Claude Code
  apiclaude run --help   # 查看 Claude Code 帮助
  apiclaude add          # 添加新节点
  apiclaude remove node1 # 删除名为 node1 的节点
  apiclaude current      # 查看当前使用的节点
""")


def main():
    config = load_config()

    if len(sys.argv) == 1:
        # 无参数：选择节点后启动 Claude Code
        sys.exit(select_node(config, launch=True))
    elif sys.argv[1] in ["select", "switch"]:
        sys.exit(select_node(config, launch=True, claude_args=sys.argv[2:]))
    elif sys.argv[1] == "run":
        sys.exit(use_current_node(config, launch=True, claude_args=sys.argv[2:]))
    elif sys.argv[1] == "list":
        list_nodes(config)
    elif sys.argv[1] == "add":
        add_node(config)
    elif sys.argv[1] == "remove":
        if len(sys.argv) < 3:
            print("错误：请指定要删除的节点名称")
            print("用法: apiclaude remove <节点名称>")
        else:
            remove_node(config, sys.argv[2])
    elif sys.argv[1] == "current":
        show_current(config)
    elif sys.argv[1] in ["help", "-h", "--help"]:
        print_usage()
    else:
        # 其它参数按 Claude Code 参数处理，例如:
        # apiclaude --permission-mode bypassPermissions
        # apiclaude resume
        sys.exit(select_node(config, launch=True, claude_args=sys.argv[1:]))


if __name__ == "__main__":
    main()
