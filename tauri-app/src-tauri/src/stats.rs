use rusqlite::{Connection, Result as SqlResult};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct NodeUsageStats {
    pub node_name: String,
    pub total_tokens: i64,
    pub request_count: i32,
    pub last_used_at: Option<String>,
    pub daily_trend: Vec<DailyUsagePoint>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct DailyUsagePoint {
    pub date: String,
    pub tokens: i64,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct UsageOverview {
    pub today_total_tokens: i64,
    pub today_request_count: i32,
    pub week_total_tokens: i64,
    pub week_request_count: i32,
    pub top_nodes: Vec<NodeUsageStats>,
}

fn get_db_path() -> PathBuf {
    let home = dirs::home_dir().expect("无法获取用户主目录");
    home.join(".token-stats.db")
}

fn open_db() -> SqlResult<Connection> {
    let path = get_db_path();

    if !path.exists() {
        // 数据库不存在，返回空连接
        return Err(rusqlite::Error::InvalidPath(path));
    }

    Connection::open(path)
}

/// 获取单个节点的使用统计
#[tauri::command]
pub fn get_node_usage_stats(node_name: String) -> Result<Option<NodeUsageStats>, String> {
    let conn = match open_db() {
        Ok(c) => c,
        Err(_) => {
            // 数据库不存在或无法打开
            return Ok(None);
        }
    };

    let mut stmt = conn.prepare(
        "SELECT node_name, total_tokens, request_count, last_used_at, daily_data
         FROM node_usage
         WHERE node_name = ?1"
    ).map_err(|e| format!("查询失败: {}", e))?;

    let result = stmt.query_row([&node_name], |row| {
        let daily_data_json: Option<String> = row.get(4)?;
        let daily_trend = if let Some(json) = daily_data_json {
            serde_json::from_str(&json).unwrap_or_default()
        } else {
            Vec::new()
        };

        Ok(NodeUsageStats {
            node_name: row.get(0)?,
            total_tokens: row.get(1)?,
            request_count: row.get(2)?,
            last_used_at: row.get(3)?,
            daily_trend,
        })
    });

    match result {
        Ok(stats) => Ok(Some(stats)),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(e) => Err(format!("查询错误: {}", e)),
    }
}

/// 获取所有节点的使用统计（排序后的前 N 个）
#[tauri::command]
pub fn get_all_nodes_usage(limit: i32) -> Result<Vec<NodeUsageStats>, String> {
    let conn = match open_db() {
        Ok(c) => c,
        Err(_) => {
            // 数据库不存在，返回空列表
            return Ok(Vec::new());
        }
    };

    let mut stmt = conn.prepare(
        "SELECT node_name, total_tokens, request_count, last_used_at, daily_data
         FROM node_usage
         ORDER BY total_tokens DESC
         LIMIT ?1"
    ).map_err(|e| format!("查询失败: {}", e))?;

    let rows = stmt.query_map([limit], |row| {
        let daily_data_json: Option<String> = row.get(4)?;
        let daily_trend = if let Some(json) = daily_data_json {
            serde_json::from_str(&json).unwrap_or_default()
        } else {
            Vec::new()
        };

        Ok(NodeUsageStats {
            node_name: row.get(0)?,
            total_tokens: row.get(1)?,
            request_count: row.get(2)?,
            last_used_at: row.get(3)?,
            daily_trend,
        })
    }).map_err(|e| format!("查询错误: {}", e))?;

    let mut results = Vec::new();
    for row in rows {
        if let Ok(stats) = row {
            results.push(stats);
        }
    }

    Ok(results)
}

/// 获取使用概览（今日、本周统计）
#[tauri::command]
pub fn get_usage_overview() -> Result<UsageOverview, String> {
    let conn = match open_db() {
        Ok(c) => c,
        Err(_) => {
            // 数据库不存在，返回空统计
            return Ok(UsageOverview {
                today_total_tokens: 0,
                today_request_count: 0,
                week_total_tokens: 0,
                week_request_count: 0,
                top_nodes: Vec::new(),
            });
        }
    };

    // 获取今日统计
    let today = chrono::Local::now().format("%Y-%m-%d").to_string();
    let mut today_stmt = conn.prepare(
        "SELECT COALESCE(SUM(total_tokens), 0), COALESCE(SUM(request_count), 0)
         FROM node_usage
         WHERE date(last_used_at) = ?1"
    ).map_err(|e| format!("查询今日统计失败: {}", e))?;

    let (today_tokens, today_requests): (i64, i32) = today_stmt.query_row([&today], |row| {
        Ok((row.get(0)?, row.get(1)?))
    }).unwrap_or((0, 0));

    // 获取本周统计
    let week_ago = chrono::Local::now() - chrono::Duration::days(7);
    let week_ago_str = week_ago.format("%Y-%m-%d").to_string();
    let mut week_stmt = conn.prepare(
        "SELECT COALESCE(SUM(total_tokens), 0), COALESCE(SUM(request_count), 0)
         FROM node_usage
         WHERE date(last_used_at) >= ?1"
    ).map_err(|e| format!("查询本周统计失败: {}", e))?;

    let (week_tokens, week_requests): (i64, i32) = week_stmt.query_row([&week_ago_str], |row| {
        Ok((row.get(0)?, row.get(1)?))
    }).unwrap_or((0, 0));

    // 获取 Top 5 节点
    let top_nodes = get_all_nodes_usage(5)?;

    Ok(UsageOverview {
        today_total_tokens: today_tokens,
        today_request_count: today_requests,
        week_total_tokens: week_tokens,
        week_request_count: week_requests,
        top_nodes,
    })
}

/// 检查统计数据库是否可用
#[tauri::command]
pub fn check_stats_available() -> Result<bool, String> {
    let path = get_db_path();
    Ok(path.exists())
}
