# 快速参考：使用统计功能

## 🎯 一句话总结

API Node Manager 可以读取 Token 记录应用写入的 SQLite 数据库，展示节点使用统计，**无需两个应用同时运行**。

---

## 📁 关键文件

### API Node Manager（已完成）
```
src-tauri/
  ├── Cargo.toml              (添加 rusqlite 依赖)
  ├── src/
      ├── lib.rs              (注册命令)
      └── stats.rs            (SQLite 读取逻辑)

src/
  ├── index.html              (统计 UI)
  ├── style.css               (统计样式)
  └── app.js                  (统计逻辑)
```

### Token 记录应用（待实现）
```
electron/
  ├── stats-writer.ts         (SQLite 写入逻辑) ← 需要创建
  └── main.ts                 (初始化数据库) ← 添加几行

数据库位置: ~/.token-stats.db
```

---

## 🔄 数据流

```
Token 记录应用扫描日志
    ↓
recordNodeUsage(nodeName, tokens, timestamp)
    ↓
写入 ~/.token-stats.db
    ↓
API Node Manager 读取
    ↓
显示统计图表
```

---

## 🛠️ 你需要做的 3 步

### 1. 安装依赖
```bash
cd "F:\vscode代码\agent token 记录"
npm install better-sqlite3
```

### 2. 创建写入模块
复制 `TOKEN-STATS-INTEGRATION.md` 中的代码到 `electron/stats-writer.ts`

### 3. 集成到主进程
在 `electron/main.ts` 添加：
```typescript
import { initStatsDB, recordNodeUsage } from './stats-writer';

app.whenReady().then(() => {
    initStatsDB();  // 初始化数据库
});

// 在扫描逻辑中调用
recordNodeUsage(nodeName, tokens, timestamp);
```

---

## 📊 API Node Manager 提供的命令

### Rust 命令
```rust
check_stats_available() -> bool
get_node_usage_stats(node_name: String) -> NodeUsageStats
get_all_nodes_usage(limit: i32) -> Vec<NodeUsageStats>
get_usage_overview() -> UsageOverview
```

### JavaScript 调用
```javascript
const available = await API.checkStatsAvailable();
const stats = await API.getNodeUsageStats('bohe-relay');
const all = await API.getAllNodesUsage(10);
const overview = await API.getUsageOverview();
```

---

## 🗄️ 数据库结构

```sql
CREATE TABLE node_usage (
    node_name TEXT PRIMARY KEY,
    total_tokens INTEGER,
    request_count INTEGER,
    last_used_at TEXT,
    daily_data TEXT  -- JSON数组
);
```

### 示例数据
```
bohe-relay      | 125000 | 45  | 2026-06-23T15:30:00Z
anthropic       | 88500  | 32  | 2026-06-23T14:20:00Z
```

---

## 🎨 UI 预览

### 数据不可用
```
┌───────────────────────────┐
│ ⓘ 统计数据不可用           │
│ 需要运行 Token 记录应用    │
└───────────────────────────┘
```

### 数据可用
```
┌─────────────────────────────┐
│ 📊 今日: 125.5K  (45次)     │
│ 📈 本周: 890.2K  (312次)    │
├─────────────────────────────┤
│ 🥇 bohe-relay     250.5K    │
│ 🥈 anthropic      180.3K    │
│ 🥉 openai         120.8K    │
└─────────────────────────────┘
```

---

## ✅ 测试步骤

### 1. 编译 API Node Manager
```bash
cd tauri-app
npm run tauri dev
```

### 2. 检查默认状态
- 打开设置页
- 应显示「统计数据不可用」

### 3. 在 Token 记录应用写入测试数据
```typescript
import { recordNodeUsage } from './stats-writer';

recordNodeUsage('test-node', 10000, new Date().toISOString());
```

### 4. 刷新 API Node Manager
- 点击「刷新统计」按钮
- 应显示测试数据

---

## 🐛 故障排查

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 显示"不可用" | 数据库不存在 | 运行 Token 记录应用写入数据 |
| 数据不更新 | 未调用刷新 | 点击刷新按钮 |
| 节点名称为空 | 未正确提取 | 检查 extractNodeName() 逻辑 |

---

## 📚 完整文档

- **TOKEN-STATS-INTEGRATION.md** - 详细实现指南
- **STATS-SUMMARY.md** - 功能完整总结
- **INTEGRATION-PLAN.md** - 三种方案对比

---

## 💡 提示

- 数据库位置：`~/.token-stats.db`
- 推荐每条请求都立即写入
- SQLite 性能足够，无需批量
- Token 格式化：K (千), M (百万)

---

**预计工作量**：30 分钟
**代码行数**：~100 行 TypeScript
