# 🎯 Tauri 应用使用流程

## 目录导航

```
tauri-app/
├── 📖 文档指南
│   ├── README.md         ← 从这里开始
│   ├── QUICK_START.md    ← 详细启动说明
│   └── GUIDE.md          ← 开发指南和技术细节
│
└── 🚀 快速启动
    ├── dev.bat / dev.sh      ← 双击运行开发模式
    └── build.bat / build.sh  ← 双击构建正式版
```

## 📋 快速使用流程

### 步骤 1: 选择你的角色

#### 👨‍💻 开发者（修改代码）
```
1. 双击运行 dev.bat (Windows) 或 ./dev.sh (macOS/Linux)
2. 等待应用窗口打开（首次约 3-5 分钟）
3. 修改代码，应用自动刷新
4. 按 Ctrl+C 停止
```

#### 📦 打包者（构建发布版）
```
1. 双击运行 build.bat (Windows) 或 ./build.sh (macOS/Linux)
2. 等待构建完成（首次约 5-10 分钟）
3. 在 src-tauri/target/release/ 找到 exe/app
4. 可选：构建完成后立即运行
```

#### 👤 最终用户（使用应用）
```
直接运行构建好的 .exe / .app 文件
无需安装任何开发工具
```

### 步骤 2: 应用使用

```
1. 点击「添加节点」
   ├─ 填写节点名称
   ├─ 填写 Base URL
   └─ 填写 API Token

2. 点击「启动」按钮
   ├─ 选择工作目录
   ├─ 配置启动选项（模式/权限/模型）
   └─ 确认启动

3. 新终端窗口打开
   └─ Claude/Codex 使用选中的 API 节点运行
```

## 🔄 开发工作流

```
┌─────────────────┐
│  修改代码       │
│  (src/ 或       │
│   src-tauri/)   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐      ┌─────────────────┐
│  dev.bat/sh     │──→──│  自动重新编译   │
│  运行中...      │      │  + 热重载       │
└────────┬────────┘      └─────────────────┘
         │
         ▼
┌─────────────────┐
│  测试新功能     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  满意？         │
│  ├─ 是 → 提交   │
│  └─ 否 → 继续改 │
└─────────────────┘
```

## 📁 文件说明速查

| 文件 | 用途 | 何时使用 |
|------|------|---------|
| **dev.bat/sh** | 开发模式 | 开发时每次启动 |
| **build.bat/sh** | 构建正式版 | 准备发布时 |
| **README.md** | 项目概览 | 第一次看项目 |
| **QUICK_START.md** | 详细使用说明 | 遇到问题时 |
| **GUIDE.md** | 技术文档 | 学习架构和开发 |

## ⚙️ 配置文件位置

用户配置文件（与 CLI 和 Web 版共享）：

```
Windows:
  C:\Users\你的用户名\.apiclaude_config.json
  C:\Users\你的用户名\.codex-api\profiles.json

macOS/Linux:
  ~/.apiclaude_config.json
  ~/.codex-api/profiles.json
```

## 🆘 遇到问题？

### 按顺序检查：

1. **查看 QUICK_START.md** → 常见问题解答
2. **查看终端输出** → 错误信息
3. **确认环境**:
   - Node.js 已安装？运行 `node --version`
   - Rust 已安装？运行 `cargo --version`
4. **重新安装依赖**:
   ```bash
   rm -rf node_modules
   npm install
   ```

### 最常见问题

| 问题 | 解决方案 |
|------|---------|
| npm 不是命令 | 安装 Node.js: https://nodejs.org/ |
| cargo 不是命令 | 安装 Rust: https://rustup.rs/ |
| 编译卡住很久 | 正常！首次编译需要 3-5 分钟 |
| 找不到 exe | 看 `src-tauri/target/release/` |

## 🎓 学习路径

### 新手（只想使用）
1. 阅读 README.md
2. 双击 dev.bat/sh
3. 开始使用应用

### 开发者（想修改）
1. 阅读 QUICK_START.md
2. 阅读 GUIDE.md 的"技术对比"部分
3. 修改 `src/app.js` 试试
4. 查看效果（自动刷新）

### 高级开发（深入定制）
1. 阅读完整 GUIDE.md
2. 学习 Tauri 文档: https://tauri.app/
3. 学习 Rust: https://www.rust-lang.org/learn
4. 修改 `src-tauri/src/*.rs`

## 🌟 推荐阅读顺序

```
首次使用:
  README.md → QUICK_START.md → 运行 dev.bat/sh

开发时:
  GUIDE.md → 修改代码 → 测试

发布前:
  build.bat/sh → 测试 exe → 分发

遇到问题:
  QUICK_START.md 常见问题 → 终端输出 → GitHub Issues
```

## ✨ 快速参考

### 运行命令

```bash
# 开发模式（热重载）
npm run dev

# 构建正式版
npm run build

# 仅编译 Rust（不启动）
cd src-tauri
cargo build --release
```

### 目录说明

- `src/` - 前端代码（HTML/CSS/JS）
- `src-tauri/` - Rust 后端
- `src-tauri/target/release/` - 构建产物（exe/app）
- `node_modules/` - npm 依赖（可删除重新安装）

### 快捷操作

```bash
# 清理构建缓存
cd src-tauri
cargo clean

# 清理 npm 依赖
rm -rf node_modules
npm install

# 查看 Rust 编译进度
cd src-tauri
cargo build --release --verbose
```

---

**提示**: 所有 .bat 和 .sh 脚本都可以直接双击运行，会自动处理依赖和环境检查。
