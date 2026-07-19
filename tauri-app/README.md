# API Node Manager - Tauri Desktop App

跨平台桌面应用，用于管理 Claude Code 和 Codex CLI 的 API 配置并一键启动。

## 🚀 快速开始

### Windows 用户
双击运行 `dev.bat`（开发模式）或 `build.bat`（构建正式版）

### macOS / Linux 用户
运行 `./dev.sh`（开发模式）或 `./build.sh`（构建正式版）

**📖 详细说明请查看 [QUICK_START.md](./QUICK_START.md)**

## 功能特性

- **节点管理** — 增删改查 Claude 节点和 Codex 配置
- **可视化界面** — macOS 风格的毛玻璃 UI，统计卡片、表格视图、详情面板
- **一键启动** — 点击启动按钮，选择工作目录，配置启动参数，在新终端窗口中打开 CLI
- **Token 脱敏** — API Token 在界面中自动脱敏显示（sk-xxx***xxx）
- **启动配置** — Claude 支持新对话/Resume、权限模式选择（默认/完全访问/沙盒）、模型选择
- **跨平台** — 支持 Windows、macOS、Linux

## 技术栈

- **前端** — HTML + CSS + JavaScript (ES Modules)
- **后端** — Rust + Tauri 2
- **存储** — 复用 `apiagent.py` 的配置文件：
  - Claude: `~/.apiclaude_config.json`
  - Codex: `~/.codex-api/profiles.json`

## 环境要求

- Rust 1.70+
- Node.js 18+
- Claude Code CLI 或 Codex CLI 已安装并在 PATH 中

## 开发

### 安装依赖

```bash
cd tauri-app
npm install
```

### 运行开发模式

```bash
npm run dev
```

### 构建应用

```bash
npm run build
```

构建产物在 `src-tauri/target/release/` 目录下。

## 使用说明

### 添加节点

1. 点击顶部「添加节点」按钮
2. 填写节点名称、Base URL、API Token
3. 确认添加

### 启动 Claude/Codex

1. 在节点列表中点击「启动」按钮（绿色）
2. **系统文件夹对话框弹出**，选择工作目录
3. **启动配置弹窗出现**，设置：
   - **工作目录**：已选目录，可重新选择
   - **启动模式**（仅 Claude）：新对话 / Resume 上次会话
   - **权限模式**（仅 Claude）：默认 / 完全访问（bypassPermissions）/ 沙盒
   - **模型选择**（仅 Claude）：可选择 Opus 4.8/4.7/4.6, Sonnet 4.6, Haiku 4.5, Fable 5，或使用默认模型
4. 点击「确认启动」
5. 新终端窗口打开，Claude/Codex 启动并使用选中的 API 节点

### 切换节点

点击「切换」按钮，只更新 current 标记，不启动终端。下次在命令行运行 `apiclaude` 或 `apicodex` 会使用切换后的节点。

## 终端检测

### Windows
- 优先使用 **Windows Terminal** (wt)
- 未检测到则 fallback 到传统的 **cmd.exe**

### macOS
- 使用系统默认的 **Terminal.app**

### Linux
- 依次尝试：gnome-terminal, konsole, xfce4-terminal, xterm

## 环境变量处理

**重要说明：**
- 启动功能会在**新终端进程中**设置环境变量（ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, CODEX_HOME）
- **不会修改当前会话的环境变量**
- 不会影响你已登录的账号环境
- 每个启动的终端都是独立的进程，互不干扰

## 项目结构

```
tauri-app/
├── src/                    # 前端源码
│   ├── index.html         # 主页面
│   ├── style.css          # 样式
│   └── app.js             # 交互逻辑
├── src-tauri/             # Rust 后端
│   ├── src/
│   │   ├── lib.rs         # 入口
│   │   ├── config.rs      # 配置文件读写
│   │   └── commands.rs    # Tauri 命令
│   ├── Cargo.toml         # Rust 依赖
│   └── tauri.conf.json    # Tauri 配置
└── package.json           # Node.js 依赖
```

## License

与父项目相同

