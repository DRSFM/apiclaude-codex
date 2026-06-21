# API Node Manager - Web UI

Web 版 API 节点管理界面，用于管理 Claude Code 和 Codex CLI 的 API 配置并一键启动。

## 功能特性

- **节点管理** — 增删改查 Claude 节点和 Codex 配置
- **可视化界面** — macOS 风格的毛玻璃 UI，统计卡片、表格视图、详情面板
- **一键启动** — 点击启动按钮，选择工作目录，配置启动参数，在新终端窗口中打开 CLI
- **Token 脱敏** — API Token 在界面中自动脱敏显示（sk-xxx***xxx）
- **启动配置** — Claude 支持新对话/Resume、权限模式选择（默认/完全访问/沙盒）

## 安装和启动

### 环境要求

- Python 3.9+
- FastAPI, uvicorn
- tkinter（Windows 自带）
- Claude Code CLI 或 Codex CLI 已安装并在 PATH 中

### 安装依赖

```bash
cd apiclaude-codex/web
pip install -r requirements.txt
```

### 启动服务

**Windows:**
```
start.bat
```

**Linux/macOS:**
```bash
chmod +x start.sh
./start.sh
```

服务将在 `http://127.0.0.1:5000` 启动。

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

### 终端检测

- 优先使用 **Windows Terminal** (wt)
- 未检测到则 fallback 到传统的 **cmd.exe**

## 环境变量处理

**重要说明：**
- 启动功能会在**新终端进程中**设置环境变量（ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, CODEX_HOME）
- **不会修改当前会话的环境变量**
- 不会影响你已登录的账号环境
- 每个启动的终端都是独立的进程，互不干扰

**实际执行的命令示例：**

Claude (Windows Terminal):
```
wt -d "C:\project" -- cmd /k "set ANTHROPIC_BASE_URL=https://api.example.com && set ANTHROPIC_AUTH_TOKEN=sk-xxx && claude --permission-mode bypassPermissions --model claude-opus-4-6"
```

Claude (cmd.exe fallback):
```
start cmd /k "cd /d C:\project && set ANTHROPIC_BASE_URL=https://api.example.com && set ANTHROPIC_AUTH_TOKEN=sk-xxx && claude resume --model claude-opus-4-6"
```

Codex:
```
wt -d "C:\project" -- cmd /k "set CODEX_HOME=C:\Users\xxx\.codex-api\profiles\bohe && codex"
```

## API 接口

### GET `/api/claude/nodes`
列出所有 Claude 节点（Token 已脱敏）

### POST `/api/claude/nodes`
添加 Claude 节点
```json
{"name": "节点名", "api_key": "sk-...", "base_url": "https://..."}
```

### DELETE `/api/claude/nodes/{name}`
删除 Claude 节点

### POST `/api/browse-folder`
弹出系统文件夹选择对话框，返回选中的路径

### POST `/api/claude/start/{name}`
启动 Claude 到新终端
```json
{
  "folder": "C:\\project",
  "mode": "new",  // "new" | "resume"
  "permission": "bypassPermissions"  // "default" | "bypassPermissions" | "sandbox"
}
```

### POST `/api/codex/start/{name}`
启动 Codex 到新终端
```json
{"folder": "C:\\project"}
```

## 架构说明

- **后端** — FastAPI (Python)，负责配置管理、文件夹选择对话框、终端启动
- **前端** — 纯 HTML + CSS + JavaScript，无框架依赖
- **存储** — 复用 `apiagent.py` 的配置文件：
  - Claude: `~/.apiclaude_config.json`
  - Codex: `~/.codex-api/profiles.json`

## 注意事项

- Windows Terminal 需要单独安装（[下载链接](https://aka.ms/terminal)）
- tkinter 文件夹选择器在 Windows 上是模态对话框，选择文件夹时浏览器会等待
- 启动后的终端窗口独立运行，关闭浏览器不影响已启动的 CLI
- 如果终端启动失败，检查 `claude` 或 `codex` 是否在 PATH 中

## 截图

（待补充）

## 开发

修改代码后，uvicorn 会自动重载（`--reload` 模式）。

前端文件：
- `frontend/index.html` — 页面结构
- `frontend/static/style.css` — 样式
- `frontend/static/app.js` — 交互逻辑

后端文件：
- `backend/app.py` — FastAPI 应用

## License

与父项目相同
