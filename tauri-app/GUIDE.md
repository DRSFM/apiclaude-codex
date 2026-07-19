# API Node Manager - Tauri 桌面应用

## 快速开始

### Windows 用户

```cmd
cd tauri-app
dev.bat
```

### macOS/Linux 用户

```bash
cd tauri-app
./dev.sh
```

或者：

```bash
cd tauri-app
npm run dev
```

## 与 Web 版本的区别

### Tauri 版本优势

1. **原生性能** — Rust 后端，比 Python FastAPI 更快
2. **独立打包** — 可以打包成 .exe / .app / .deb 独立运行，无需安装 Python 环境
3. **系统集成** — 原生文件选择对话框，更好的系统托盘支持（未来可添加）
4. **更小体积** — 打包后约 10-15MB（Web 版需要整个 Python 运行时）
5. **跨平台一致性** — Windows/macOS/Linux 使用相同的代码库

### Web 版本优势

1. **快速部署** — 无需编译，直接运行 Python 脚本
2. **远程访问** — 可以通过浏览器远程访问
3. **易于调试** — Python 代码更容易快速修改和测试

## 技术对比

| 特性 | Web 版 | Tauri 版 |
|------|--------|----------|
| 后端语言 | Python + FastAPI | Rust |
| 前端技术 | HTML + CSS + JS | HTML + CSS + JS |
| 启动方式 | 浏览器访问 localhost:5000 | 独立桌面应用 |
| 打包方式 | 无（需 Python 环境） | .exe / .app / .deb |
| 内存占用 | ~100MB (Python + uvicorn) | ~50MB (Rust + WebView) |
| 启动速度 | ~1-2秒 | ~0.5秒 |
| 文件对话框 | Python tkinter | 系统原生 |
| 终端启动 | subprocess | 系统原生 Command/Process |

## 开发指南

### 目录结构

```
tauri-app/
├── src/                      # 前端代码
│   ├── index.html           # 主页面
│   ├── style.css            # 从 web/frontend 复制
│   └── app.js               # 适配 Tauri API 的前端逻辑
├── src-tauri/               # Rust 后端
│   ├── src/
│   │   ├── lib.rs          # Tauri 入口
│   │   ├── config.rs       # 配置文件读写（复用 Python 的配置格式）
│   │   └── commands.rs     # Tauri 命令（对应 Python 的 API 端点）
│   ├── Cargo.toml          # Rust 依赖
│   └── tauri.conf.json     # Tauri 配置
├── dev.bat / dev.sh        # 快速启动脚本
└── package.json            # Node.js 依赖
```

### 前端 API 调用对比

**Web 版 (fetch):**
```javascript
const nodes = await fetch('/api/claude/nodes').then(r => r.json());
```

**Tauri 版 (invoke):**
```javascript
const invoke = (...args) => window.__TAURI__.core.invoke(...args);
const nodes = await invoke('get_claude_nodes');
```

### 后端实现对比

**Web 版 (Python FastAPI):**
```python
@app.get("/api/claude/nodes")
async def get_claude_nodes():
    config = load_claude_config()
    return config.nodes
```

**Tauri 版 (Rust):**
```rust
#[tauri::command]
pub async fn get_claude_nodes() -> Result<ClaudeNodesResponse, String> {
    let config = load_claude_config()?;
    // returns { nodes, current }
}
```

## 配置文件兼容性

Tauri 版本**完全兼容** Python 版本的配置文件：

- `~/.apiclaude_config.json` — Claude 节点配置
- `~/.codex-api/profiles.json` — Codex 配置

你可以同时使用 CLI 工具、Web 版和 Tauri 版，它们共享相同的配置文件。

## 构建发布版

```bash
cd tauri-app
npm run build
```

构建产物位置：
- **Windows**: `src-tauri/target/release/api-node-manager.exe`
- **macOS**: `src-tauri/target/release/bundle/macos/API Node Manager.app`
- **Linux**: `src-tauri/target/release/bundle/deb/api-node-manager_0.1.0_amd64.deb`

## 下一步改进

- [ ] 添加系统托盘图标，后台常驻
- [ ] 支持快捷键快速切换节点
- [ ] 添加自动更新功能
- [ ] 支持导入/导出配置
- [ ] 添加使用统计（启动次数、使用时长）
- [ ] 支持主题切换（亮色/暗色）
- [ ] 添加 Windows/macOS 安装程序
