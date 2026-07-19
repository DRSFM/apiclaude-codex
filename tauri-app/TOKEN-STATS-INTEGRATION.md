# Token 记录应用端集成指南

## 概述

需要在 Token 记录应用中添加一个简单的 SQLite 写入逻辑，将节点使用记录存储到 `~/.token-stats.db`。

---

## 数据库结构

### 表定义

```sql
CREATE TABLE IF NOT EXISTS node_usage (
    node_name TEXT PRIMARY KEY,
    total_tokens INTEGER DEFAULT 0,
    request_count INTEGER DEFAULT 0,
    last_used_at TEXT,
    daily_data TEXT
);
```

### 字段说明

- `node_name` - 节点名称（主键）
- `total_tokens` - 累计 Token 总数
- `request_count` - 累计请求次数
- `last_used_at` - 最后使用时间（ISO 8601 格式）
- `daily_data` - 每日趋势数据（JSON 格式）

---

## 实现步骤

### 1. 安装依赖

```bash
npm install better-sqlite3
# 或
yarn add better-sqlite3
```

### 2. 创建统计写入模块

创建文件 `electron/stats-writer.ts`：

```typescript
import Database from 'better-sqlite3';
import { homedir } from 'os';
import { join } from 'path';

let db: Database.Database | null = null;

export function initStatsDB() {
    const dbPath = join(homedir(), '.token-stats.db');
    
    try {
        db = new Database(dbPath);
        
        // 创建表
        db.exec(`
            CREATE TABLE IF NOT EXISTS node_usage (
                node_name TEXT PRIMARY KEY,
                total_tokens INTEGER DEFAULT 0,
                request_count INTEGER DEFAULT 0,
                last_used_at TEXT,
                daily_data TEXT
            )
        `);
        
        console.log('Stats DB initialized:', dbPath);
    } catch (err) {
        console.error('Failed to initialize stats DB:', err);
    }
}

export function recordNodeUsage(
    nodeName: string,
    tokens: number,
    timestamp: string
) {
    if (!db) return;
    
    try {
        // 插入或更新节点统计
        const stmt = db.prepare(`
            INSERT INTO node_usage (node_name, total_tokens, request_count, last_used_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(node_name) DO UPDATE SET
                total_tokens = total_tokens + ?,
                request_count = request_count + 1,
                last_used_at = ?
        `);
        
        stmt.run(nodeName, tokens, timestamp, tokens, timestamp);
        
        // 更新每日趋势数据
        updateDailyData(nodeName, tokens, timestamp);
        
    } catch (err) {
        console.error('Failed to record node usage:', err);
    }
}

function updateDailyData(nodeName: string, tokens: number, timestamp: string) {
    if (!db) return;
    
    try {
        const date = timestamp.split('T')[0]; // YYYY-MM-DD
        
        // 读取现有 daily_data
        const row = db.prepare('SELECT daily_data FROM node_usage WHERE node_name = ?')
            .get(nodeName) as { daily_data: string | null };
        
        let dailyData: Array<{ date: string; tokens: number }> = [];
        
        if (row && row.daily_data) {
            try {
                dailyData = JSON.parse(row.daily_data);
            } catch {
                dailyData = [];
            }
        }
        
        // 更新或添加今天的数据
        const existing = dailyData.find(d => d.date === date);
        if (existing) {
            existing.tokens += tokens;
        } else {
            dailyData.push({ date, tokens });
        }
        
        // 只保留最近 30 天
        dailyData = dailyData
            .sort((a, b) => b.date.localeCompare(a.date))
            .slice(0, 30);
        
        // 写回数据库
        db.prepare('UPDATE node_usage SET daily_data = ? WHERE node_name = ?')
            .run(JSON.stringify(dailyData), nodeName);
        
    } catch (err) {
        console.error('Failed to update daily data:', err);
    }
}

export function closeStatsDB() {
    if (db) {
        db.close();
        db = null;
    }
}
```

### 3. 集成到主进程

在 `electron/main.ts` 中：

```typescript
import { initStatsDB, recordNodeUsage, closeStatsDB } from './stats-writer';

app.whenReady().then(() => {
    // ... 其他初始化
    
    // 初始化统计数据库
    initStatsDB();
});

app.on('before-quit', () => {
    // 关闭数据库
    closeStatsDB();
});
```

### 4. 在数据扫描时记录

在你的扫描逻辑中，每当解析到一条请求记录时调用 `recordNodeUsage`：

```typescript
import { recordNodeUsage } from './stats-writer';

// 在解析请求记录的地方
function processRequestRecord(record: RequestRecord) {
    // ... 你的现有逻辑
    
    // 提取节点名称（根据你的数据结构）
    const nodeName = extractNodeName(record);
    
    if (nodeName) {
        recordNodeUsage(
            nodeName,
            record.totalTokens,
            record.timestamp
        );
    }
}

// 辅助函数：从 sessionTitle 或其他字段提取节点名称
function extractNodeName(record: RequestRecord): string | null {
    // 方式 1: 从 sessionTitle 提取
    // 假设 sessionTitle 格式类似: "bohe-relay - 项目名称"
    if (record.sessionTitle) {
        const match = record.sessionTitle.match(/^([^-]+)/);
        if (match) {
            return match[1].trim();
        }
    }
    
    // 方式 2: 从环境变量或配置中获取
    // 如果你的日志中记录了使用的节点名称
    
    // 方式 3: 使用 source 作为节点名称
    return record.source;
}
```

---

## 数据示例

### 插入示例

```typescript
recordNodeUsage('bohe-relay', 15000, '2026-06-23T10:30:00Z');
recordNodeUsage('anthropic-direct', 8500, '2026-06-23T10:31:00Z');
recordNodeUsage('bohe-relay', 12000, '2026-06-23T10:32:00Z');
```

### 数据库内容示例

```sql
SELECT * FROM node_usage;
```

| node_name | total_tokens | request_count | last_used_at | daily_data |
|-----------|--------------|---------------|--------------|------------|
| bohe-relay | 27000 | 2 | 2026-06-23T10:32:00Z | [{"date":"2026-06-23","tokens":27000}] |
| anthropic-direct | 8500 | 1 | 2026-06-23T10:31:00Z | [{"date":"2026-06-23","tokens":8500}] |

---

## 节点名称提取策略

根据你的数据结构，有几种提取节点名称的方式：

### 策略 1: 从 sessionTitle 提取（推荐）

如果你的会话标题包含节点信息：

```typescript
// sessionTitle: "bohe-relay - 项目A"
const nodeName = record.sessionTitle?.split('-')[0]?.trim();
```

### 策略 2: 从环境变量提取

如果启动 CLI 时设置了环境变量：

```typescript
// 假设日志中记录了环境变量
const nodeName = record.metadata?.API_NODE_NAME;
```

### 策略 3: 从配置文件路径推断

如果不同节点使用不同的配置文件：

```typescript
// 从文件路径推断
// ~/.claude-profiles/bohe-relay/auth.json -> bohe-relay
const pathMatch = record.filePath?.match(/\.claude-profiles\/([^/]+)/);
const nodeName = pathMatch?.[1];
```

### 策略 4: 使用 source 作为节点名

最简单的方式（但不够精确）：

```typescript
const nodeName = record.source; // 'claude-code', 'codex' 等
```

---

## 测试

### 1. 手动插入测试数据

```typescript
import { recordNodeUsage } from './stats-writer';

// 测试插入
recordNodeUsage('test-node', 10000, new Date().toISOString());
```

### 2. 验证数据库

使用 SQLite 客户端查看：

```bash
sqlite3 ~/.token-stats.db
```

```sql
SELECT * FROM node_usage;
SELECT node_name, total_tokens, request_count FROM node_usage ORDER BY total_tokens DESC;
```

### 3. 在 API Node Manager 中查看

启动 API Node Manager，进入设置页面，应该能看到统计数据。

---

## 注意事项

1. **性能考虑**
   - SQLite 写入很快，不需要批量处理
   - 建议每条记录都直接写入
   - `ON CONFLICT` 语句自动处理并发

2. **错误处理**
   - 写入失败不应该影响主要功能
   - 已经包含了 try-catch 保护

3. **数据清理**
   - 每日趋势只保留最近 30 天
   - 可以定期清理旧数据

4. **数据库位置**
   - 使用 `~/.token-stats.db`（用户主目录）
   - 与 API Node Manager 约定的位置一致

---

## 后续优化（可选）

### 1. 添加更多统计维度

```sql
-- 扩展表结构
ALTER TABLE node_usage ADD COLUMN avg_tokens_per_request REAL;
ALTER TABLE node_usage ADD COLUMN peak_hour INTEGER;
```

### 2. 定期清理旧数据

```typescript
export function cleanOldStats() {
    if (!db) return;
    
    const thirtyDaysAgo = new Date();
    thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30);
    
    db.prepare('DELETE FROM node_usage WHERE date(last_used_at) < ?')
        .run(thirtyDaysAgo.toISOString().split('T')[0]);
}
```

### 3. 导出统计报告

```typescript
export function exportStatsReport() {
    if (!db) return null;
    
    const rows = db.prepare('SELECT * FROM node_usage ORDER BY total_tokens DESC')
        .all();
    
    return {
        exported_at: new Date().toISOString(),
        total_nodes: rows.length,
        nodes: rows,
    };
}
```

---

## 完成后的效果

在 API Node Manager 的设置页面中，用户将看到：

- ✅ 今日 Token 总量和请求数
- ✅ 本周 Token 总量和请求数
- ✅ Top 5 节点排行（按 Token 消耗）
- ✅ 每个节点的历史趋势数据

**不需要两个应用同时运行**：
- Token 记录应用在后台收集数据
- API Node Manager 随时可以读取统计

---

**开发时间估算**：约 30 分钟  
**代码量**：约 100 行（含测试）

有任何问题随时告诉我！
