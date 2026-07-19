# API Node Manager 与 Token 记录应用集成方案

## 概述

将 `agent token 记录` 应用的统计功能集成到 `API Node Manager` 中，实现节点使用情况的可视化追踪。

---

## 方案 A：IPC 通信（推荐）

### 架构
```
┌──────────────────────────┐        IPC         ┌──────────────────────────┐
│  API Node Manager        │◄──────────────────►│  Token 记录应用          │
│  (Tauri)                 │                     │  (Electron)              │
│                          │                     │                          │
│  ┌────────────────────┐  │                     │  ┌────────────────────┐  │
│  │ 设置页 - 使用统计 │  │   Query Usage       │  │  TokenAPI           │  │
│  │                    │──┼────────────────────►│  │  - getOverviewStats │  │
│  │  📊 今日 Token    │  │                     │  │  - getDailyTrend    │  │
│  │  📈 趋势图表      │◄─┼─────────────────────┤  │  - getSessionRanking│  │
│  │  🏆 节点排行      │  │   Return Stats      │  └────────────────────┘  │
│  └────────────────────┘  │                     │                          │
└──────────────────────────┘                     └──────────────────────────┘
```

### 实现步骤

#### 1. Token 记录应用端（需要你添加）

在 `electron/ipc-handlers.ts` 中添加新的 IPC 方法：

```typescript
// 新增：按节点名称查询统计
ipcMain.handle('get-node-usage-stats', async (event, nodeName: string, range: DateRange) => {
  // 从现有的 RequestRecord 数据中筛选
  // sessionTitle 或 metadata 中包含节点名称的记录
  const records = await getAllRequestRecords(range);
  
  const nodeRecords = records.filter(r => 
    r.sessionTitle?.includes(nodeName) || 
    r.metadata?.nodeName === nodeName
  );
  
  return {
    nodeName,
    totalTokens: sum(nodeRecords.map(r => r.totalTokens)),
    requestCount: nodeRecords.length,
    lastUsedAt: max(nodeRecords.map(r => r.timestamp)),
    dailyTrend: aggregateByDay(nodeRecords),
  };
});

// 新增：获取所有节点的使用概览
ipcMain.handle('get-all-nodes-usage', async (event, range: DateRange) => {
  const records = await getAllRequestRecords(range);
  
  // 按节点名称分组统计
  const byNode = groupBy(records, r => r.metadata?.nodeName || 'unknown');
  
  return Object.entries(byNode).map(([name, recs]) => ({
    nodeName: name,
    totalTokens: sum(recs.map(r => r.totalTokens)),
    requestCount: recs.length,
    lastUsedAt: max(recs.map(r => r.timestamp)),
  }));
});
```

#### 2. 通信协议定义

创建共享的类型定义：

```typescript
// shared/types.ts (两边都引用)
export interface NodeUsageStats {
  nodeName: string;
  totalTokens: number;
  requestCount: number;
  lastUsedAt: string;
  dailyTrend: Array<{ date: string; tokens: number }>;
}

export interface NodeUsageRequest {
  nodeName: string;
  range: DateRange;
}
```

#### 3. API Node Manager 端实现

在 Tauri 中调用外部 Electron 应用：

```typescript
// src-tauri/src/token_stats.rs
use serde::{Deserialize, Serialize};
use std::process::Command;

#[derive(Serialize, Deserialize)]
pub struct NodeUsageStats {
    node_name: String,
    total_tokens: u64,
    request_count: u32,
    last_used_at: String,
}

#[tauri::command]
pub async fn get_node_usage_stats(node_name: String) -> Result<NodeUsageStats, String> {
    // 方式 1: HTTP API
    let client = reqwest::Client::new();
    let response = client
        .post("http://localhost:8765/api/node-usage")
        .json(&serde_json::json!({
            "nodeName": node_name,
            "range": { "kind": "last-n-days", "days": 30 }
        }))
        .send()
        .await
        .map_err(|e| format!("Token 记录应用未运行: {}", e))?;
    
    let stats = response.json::<NodeUsageStats>().await
        .map_err(|e| e.to_string())?;
    
    Ok(stats)
}
```

前端调用：

```javascript
// src/app.js
async function loadNodeUsageStats(nodeName) {
    try {
        const stats = await invoke('get_node_usage_stats', { nodeName });
        return stats;
    } catch (err) {
        console.warn('Token 记录应用未连接:', err);
        return null;
    }
}
```

---

## 方案 B：共享数据库（轻量级）

### 架构
```
┌──────────────────────────┐
│  Token 记录应用          │
│  写入使用记录             │
└────────────┬─────────────┘
             │ Write
             ▼
     ┌───────────────┐
     │  SQLite DB    │
     │  ~/.token-    │
     │  stats.db     │
     └───────┬───────┘
             │ Read
             ▼
┌────────────┴─────────────┐
│  API Node Manager        │
│  读取并展示统计           │
└──────────────────────────┘
```

### 实现步骤

#### 1. Token 记录应用写入（需要你添加）

```typescript
// electron/stats-writer.ts
import Database from 'better-sqlite3';
import { homedir } from 'os';
import { join } from 'path';

const db = new Database(join(homedir(), '.token-stats.db'));

// 初始化表
db.exec(`
  CREATE TABLE IF NOT EXISTS node_usage (
    node_name TEXT PRIMARY KEY,
    total_tokens INTEGER DEFAULT 0,
    request_count INTEGER DEFAULT 0,
    last_used_at TEXT,
    daily_data TEXT
  )
`);

// 每次记录新请求时更新
export function recordNodeUsage(nodeName: string, tokens: number, timestamp: string) {
    const stmt = db.prepare(`
        INSERT INTO node_usage (node_name, total_tokens, request_count, last_used_at)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(node_name) DO UPDATE SET
            total_tokens = total_tokens + ?,
            request_count = request_count + 1,
            last_used_at = ?
    `);
    
    stmt.run(nodeName, tokens, timestamp, tokens, timestamp);
}
```

#### 2. API Node Manager 读取

```rust
// src-tauri/src/token_stats.rs
use rusqlite::Connection;
use std::env;

#[tauri::command]
pub fn get_node_usage_stats(node_name: String) -> Result<NodeUsageStats, String> {
    let home = env::var("USERPROFILE")
        .or_else(|_| env::var("HOME"))
        .map_err(|_| "无法获取用户目录")?;
    
    let db_path = format!("{}/.token-stats.db", home);
    let conn = Connection::open(db_path)
        .map_err(|e| format!("无法打开统计数据库: {}", e))?;
    
    let mut stmt = conn.prepare(
        "SELECT total_tokens, request_count, last_used_at 
         FROM node_usage WHERE node_name = ?"
    ).map_err(|e| e.to_string())?;
    
    let stats = stmt.query_row([&node_name], |row| {
        Ok(NodeUsageStats {
            node_name: node_name.clone(),
            total_tokens: row.get(0)?,
            request_count: row.get(1)?,
            last_used_at: row.get(2)?,
        })
    }).map_err(|e| e.to_string())?;
    
    Ok(stats)
}
```

---

## 方案 C：HTTP API（最灵活）

### 架构
```
┌──────────────────────────┐     HTTP GET      ┌──────────────────────────┐
│  API Node Manager        ├──────────────────►│  Token 记录应用          │
│                          │  /api/node-usage  │  (内置 HTTP Server)      │
│  localhost:3000          │◄──────────────────┤  localhost:8765          │
└──────────────────────────┘    JSON Response  └──────────────────────────┘
```

### 实现步骤

#### 1. Token 记录应用添加 HTTP 服务（需要你添加）

```typescript
// electron/http-server.ts
import express from 'express';
import cors from 'cors';
import { getOverviewStats, getDailyTrend, getSessionRanking } from './ipc-handlers';

const app = express();
app.use(cors());
app.use(express.json());

// 节点使用统计
app.post('/api/node-usage', async (req, res) => {
    const { nodeName, range } = req.body;
    
    try {
        const sessions = await getSessionRanking(
            range,
            'tokens',
            100
        );
        
        const nodeSession = sessions.find(s => s.title === nodeName);
        
        res.json({
            nodeName,
            totalTokens: nodeSession?.totalTokens || 0,
            requestCount: nodeSession?.requestCount || 0,
            lastUsedAt: nodeSession?.lastActiveAt || null,
        });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// 所有节点概览
app.get('/api/nodes-overview', async (req, res) => {
    try {
        const range = { kind: 'last-n-days', days: 30 };
        const sessions = await getSessionRanking(range, 'tokens', 50);
        
        res.json({
            nodes: sessions.map(s => ({
                nodeName: s.title,
                totalTokens: s.totalTokens,
                requestCount: s.requestCount,
                lastUsedAt: s.lastActiveAt,
            })),
        });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// 启动服务
app.listen(8765, () => {
    console.log('Token stats HTTP server running on http://localhost:8765');
});
```

在主进程启动时初始化：

```typescript
// electron/main.ts
import { startHttpServer } from './http-server';

app.whenReady().then(() => {
    // ... 其他初始化
    startHttpServer();
});
```

#### 2. API Node Manager 调用

```javascript
// src/app.js
async function fetchNodeUsageStats(nodeName) {
    try {
        const response = await fetch('http://localhost:8765/api/node-usage', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                nodeName,
                range: { kind: 'last-n-days', days: 30 }
            })
        });
        
        if (!response.ok) {
            throw new Error('Token 记录应用未运行');
        }
        
        return await response.json();
    } catch (err) {
        console.warn('无法获取使用统计:', err);
        return null;
    }
}

async function fetchAllNodesOverview() {
    try {
        const response = await fetch('http://localhost:8765/api/nodes-overview');
        return await response.json();
    } catch (err) {
        console.warn('无法获取节点概览:', err);
        return { nodes: [] };
    }
}
```

---

## 推荐方案：**方案 C (HTTP API)**

### 优点
1. **解耦性强** - 两个应用独立运行，互不影响
2. **跨平台** - HTTP 是通用协议，未来可扩展到 Web 版
3. **易于调试** - 可以用 Postman/curl 直接测试
4. **灵活性高** - 可以轻松添加新的 API 端点
5. **防御性好** - 即使 Token 记录应用未运行，API Node Manager 也能正常工作

### 缺点
1. 需要在 Token 记录应用中添加 HTTP Server（约 50 行代码）
2. 有端口占用的可能性（可配置端口）

---

## UI 设计：设置页中的使用统计

```
┌─ 使用统计 ────────────────────────────────────┐
│                                                │
│  📊 今日总览                                  │
│  ┌──────────────────────────────────────────┐ │
│  │  Total Tokens      Requests    Avg/Req   │ │
│  │  ────────────      ────────    ────────   │ │
│  │  12.5M ↑8%         245 ↑12%    51.0K ↓2% │ │
│  └──────────────────────────────────────────┘ │
│                                                │
│  📈 30 天趋势                                 │
│  ┌──────────────────────────────────────────┐ │
│  │  ▁▂▃▅▇█▆▅▄▃▂▁ Token 使用曲线             │ │
│  └──────────────────────────────────────────┘ │
│                                                │
│  🏆 节点排行（按 Token 消耗）                 │
│  ┌──────────────────────────────────────────┐ │
│  │  1. bohe-relay        2.5M   (45%)  ████ │ │
│  │  2. anthropic-direct  1.8M   (32%)  ███  │ │
│  │  3. openai-backup     1.2M   (23%)  ██   │ │
│  └──────────────────────────────────────────┘ │
│                                                │
│  [刷新统计]  [导出报告]  [打开详细面板]      │
└────────────────────────────────────────────────┘
```

---

## 下一步

你觉得哪个方案更合适？我建议：

1. **短期**：先用 **方案 C (HTTP API)**，你在 Token 记录应用中添加一个简单的 HTTP Server
2. **长期**：考虑将统计逻辑提取成独立的 npm 包，两边都可以引用

我可以：
1. 帮你写 Token 记录应用端的 HTTP Server 代码
2. 在 API Node Manager 中实现完整的使用统计 UI
3. 设计数据同步和缓存策略

你希望我先做哪一部分？
