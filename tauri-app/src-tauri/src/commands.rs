use crate::config::{
    add_codex_project_trust, clean_hidden_prefix, codex_profile_home, get_codex_archive_root,
    get_codex_home, load_claude_config, load_codex_config, mask_secret, now_iso,
    save_claude_config, save_codex_config, slugify, write_codex_config, ClaudeNode, CodexProfile,
    DEFAULT_CODEX_BASE_URL, DEFAULT_CODEX_MODEL,
};
use crate::secure_store::{clear_secret, get_secret, set_secret};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;
use std::process::Command;

#[derive(Debug, Serialize)]
pub struct ClaudeSafeNode {
    pub base_url: String,
    pub token: String,
}

#[derive(Debug, Serialize)]
pub struct ClaudeNodesResponse {
    pub nodes: BTreeMap<String, ClaudeSafeNode>,
    pub current: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct CodexProfilesResponse {
    pub profiles: Vec<CodexProfile>,
}

#[derive(Debug, Deserialize)]
pub struct AddClaudeNodeRequest {
    pub name: String,
    #[serde(default)]
    pub base_url: String,
    pub api_key: String,
}

#[derive(Debug, Deserialize)]
pub struct AddCodexProfileRequest {
    pub name: String,
    pub api_key: String,
    #[serde(default)]
    pub base_url: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct StartClaudeRequest {
    pub folder: String,
    pub mode: String,
    pub permission: String,
    pub model: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct StartCodexRequest {
    pub folder: String,
    pub mode: String,
    pub permission: String,
    pub model: Option<String>,
}

#[tauri::command]
pub async fn get_claude_nodes() -> Result<ClaudeNodesResponse, String> {
    let config = load_claude_config()?;
    let nodes = config
        .nodes
        .iter()
        .map(|(name, node)| {
            let token = node
                .credential_id
                .as_deref()
                .and_then(|credential_id| get_secret(credential_id).ok())
                .unwrap_or_default();
            (
                name.clone(),
                ClaudeSafeNode {
                    base_url: node.base_url.clone(),
                    token: mask_secret(&token),
                },
            )
        })
        .collect();

    Ok(ClaudeNodesResponse {
        nodes,
        current: config.current,
    })
}

#[tauri::command]
pub async fn add_claude_node(request: AddClaudeNodeRequest) -> Result<(), String> {
    let name = request.name.trim();
    if name.is_empty() {
        return Err("Node name cannot be empty".to_string());
    }

    let token = clean_hidden_prefix(&request.api_key);
    if token.is_empty() {
        return Err("API token cannot be empty".to_string());
    }

    let mut config = load_claude_config()?;
    let credential_id = format!("claude:{}", name);
    set_secret(&credential_id, &token)?;
    config.nodes.insert(
        name.to_string(),
        ClaudeNode {
            base_url: clean_hidden_prefix(&request.base_url)
                .trim_end_matches('/')
                .to_string(),
            token: String::new(),
            credential_id: Some(credential_id),
        },
    );

    if config.current.is_none() {
        config.current = Some(name.to_string());
    }

    save_claude_config(&config)
}

#[tauri::command]
pub async fn remove_claude_node(name: String) -> Result<(), String> {
    let mut config = load_claude_config()?;

    let node = config
        .nodes
        .remove(&name)
        .ok_or_else(|| format!("Node '{}' not found", name))?;
    if config.current.as_deref() == Some(&name) {
        config.current = None;
    }

    save_claude_config(&config)?;
    if let Some(credential_id) = node.credential_id {
        clear_secret(&credential_id)?;
    }
    Ok(())
}

#[tauri::command]
pub async fn set_current_claude_node(name: String) -> Result<(), String> {
    let mut config = load_claude_config()?;

    if !config.nodes.contains_key(&name) {
        return Err(format!("Node '{}' not found", name));
    }

    config.current = Some(name);
    save_claude_config(&config)
}

#[tauri::command]
pub async fn start_claude(name: String, request: StartClaudeRequest) -> Result<(), String> {
    let mut config = load_claude_config()?;
    let node = config
        .nodes
        .get(&name)
        .cloned()
        .ok_or_else(|| format!("Node '{}' not found", name))?;
    let credential_id = node
        .credential_id
        .as_deref()
        .ok_or_else(|| format!("Node '{}' has no stored credential", name))?;
    let token = get_secret(credential_id)?;

    config.current = Some(name);
    save_claude_config(&config)?;

    let mut claude_args = Vec::new();

    if let Some(model) = request.model {
        let model = model.trim();
        if !model.is_empty() {
            claude_args.push("--model".to_string());
            claude_args.push(model.to_string());
        }
    }

    if request.mode == "resume" {
        claude_args.push("resume".to_string());
    }

    if request.permission != "default" {
        claude_args.push("--permission-mode".to_string());
        claude_args.push(request.permission);
    }

    launch_claude_terminal(&request.folder, &node.base_url, &token, &claude_args)
}

#[tauri::command]
pub async fn get_codex_profiles() -> Result<CodexProfilesResponse, String> {
    let config = load_codex_config()?;
    Ok(CodexProfilesResponse {
        profiles: config.profiles,
    })
}

#[tauri::command]
pub async fn add_codex_profile(request: AddCodexProfileRequest) -> Result<(), String> {
    let mut config = load_codex_config()?;
    let slug = slugify(&request.name);

    if config
        .profiles
        .iter()
        .any(|p| p.id.eq_ignore_ascii_case(&slug) || p.name.eq_ignore_ascii_case(&slug))
    {
        return Err(format!("Profile '{}' already exists", slug));
    }

    let api_key = clean_hidden_prefix(&request.api_key);
    if api_key.is_empty() {
        return Err("API key cannot be empty".to_string());
    }

    let base_url = request
        .base_url
        .as_deref()
        .map(clean_hidden_prefix)
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| DEFAULT_CODEX_BASE_URL.to_string())
        .trim_end_matches('/')
        .to_string();
    let model = request
        .model
        .as_deref()
        .map(clean_hidden_prefix)
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| DEFAULT_CODEX_MODEL.to_string());

    let home_rel = if config.profiles.is_empty() && !get_codex_home().join("auth.json").exists() {
        ".".to_string()
    } else {
        format!("profiles/{}", slug)
    };
    let home = if home_rel == "." {
        get_codex_home()
    } else {
        get_codex_home().join(&home_rel)
    };

    write_codex_config(&home, &base_url, &model)?;
    let credential_id = format!("codex:{}", slug);
    set_secret(&credential_id, &api_key)?;

    let now = now_iso();
    config.profiles.push(CodexProfile {
        id: slug.clone(),
        name: slug,
        base_url,
        home: home_rel,
        created_at: now.clone(),
        last_used_at: Some(now),
        model: Some(model),
        api_key: None,
        credential_id: Some(credential_id),
    });

    save_codex_config(&config)
}

#[tauri::command]
pub async fn remove_codex_profile(name: String) -> Result<(), String> {
    let mut config = load_codex_config()?;
    let index = config
        .profiles
        .iter()
        .position(|p| p.name == name || p.id == name)
        .ok_or_else(|| format!("Profile '{}' not found", name))?;

    let profile = config.profiles.remove(index);
    save_codex_config(&config)?;
    if let Some(credential_id) = &profile.credential_id {
        clear_secret(credential_id)?;
    }

    if profile.home != "." {
        let home = codex_profile_home(&profile);
        if home.exists() {
            let archive_root = get_codex_archive_root();
            fs::create_dir_all(&archive_root)
                .map_err(|e| format!("Failed to create Codex archive directory: {}", e))?;
            let archive_path = archive_root.join(format!(
                "{}-{}",
                profile.id,
                Utc::now().format("%Y%m%d-%H%M%S")
            ));
            fs::rename(&home, &archive_path)
                .map_err(|e| format!("Failed to archive Codex profile directory: {}", e))?;
        }
    }

    Ok(())
}

#[tauri::command]
pub async fn start_codex(name: String, request: StartCodexRequest) -> Result<(), String> {
    let mut config = load_codex_config()?;
    let index = config
        .profiles
        .iter()
        .position(|p| p.name == name || p.id == name)
        .ok_or_else(|| format!("Profile '{}' not found", name))?;

    let profile = config.profiles[index].clone();
    let home = codex_profile_home(&profile);
    let model = profile.model.as_deref().unwrap_or(DEFAULT_CODEX_MODEL);

    if !home.join("config.toml").exists() {
        write_codex_config(&home, &profile.base_url, model)?;
    }

    let credential_id = profile
        .credential_id
        .as_deref()
        .ok_or_else(|| format!("Profile '{}' has no stored credential", name))?;
    let api_key = get_secret(credential_id)?;

    add_codex_project_trust(&home, &request.folder)?;

    config.profiles[index].last_used_at = Some(now_iso());
    save_codex_config(&config)?;

    let mut codex_args = vec![
        "--disable".to_string(),
        "apps".to_string(),
        "--disable".to_string(),
        "plugins".to_string(),
    ];

    if let Some(model) = request.model {
        let model = model.trim();
        if !model.is_empty() {
            codex_args.push("--model".to_string());
            codex_args.push(model.to_string());
        }
    }

    match request.permission.as_str() {
        "bypassPermissions" => {
            codex_args.push("--dangerously-bypass-approvals-and-sandbox".to_string());
        }
        "sandbox" => {
            codex_args.push("--sandbox".to_string());
            codex_args.push("read-only".to_string());
            codex_args.push("--ask-for-approval".to_string());
            codex_args.push("on-request".to_string());
        }
        _ => {}
    }

    if request.mode == "resume" {
        codex_args.push("resume".to_string());
    }

    launch_codex_terminal(&request.folder, &home, &api_key, &codex_args)
}

fn launch_claude_terminal(
    working_dir: &str,
    base_url: &str,
    token: &str,
    args: &[String],
) -> Result<(), String> {
    let env = [
        ("ANTHROPIC_BASE_URL", base_url),
        ("ANTHROPIC_AUTH_TOKEN", token),
    ];
    launch_in_terminal(working_dir, &env, "claude", args)
}

fn launch_codex_terminal(
    working_dir: &str,
    codex_home: &Path,
    api_key: &str,
    args: &[String],
) -> Result<(), String> {
    let codex_home = codex_home.to_string_lossy();
    let env = [
        ("CODEX_HOME", codex_home.as_ref()),
        ("APICODEX_API_KEY", api_key),
    ];
    launch_in_terminal(working_dir, &env, "codex", args)
}

fn launch_in_terminal(
    working_dir: &str,
    env: &[(&str, &str)],
    command: &str,
    args: &[String],
) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        let cmd_line = build_cmd_line(command, args);
        let use_wt = Command::new("where")
            .arg("wt")
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false);

        if use_wt {
            let mut terminal = Command::new("wt");
            terminal
                .arg("-d")
                .arg(working_dir)
                .arg("--")
                .arg("cmd")
                .arg("/k")
                .arg(&cmd_line);
            for (key, value) in env {
                terminal.env(key, value);
            }
            terminal
                .spawn()
                .map_err(|e| format!("Failed to launch Windows Terminal: {}", e))?;
        } else {
            let mut terminal = Command::new("cmd");
            terminal
                .arg("/c")
                .arg("start")
                .arg("cmd")
                .arg("/k")
                .arg(format!(
                    "cd /d \"{}\" && {}",
                    escape_cmd_value(working_dir),
                    cmd_line
                ));
            for (key, value) in env {
                terminal.env(key, value);
            }
            terminal
                .spawn()
                .map_err(|e| format!("Failed to launch cmd: {}", e))?;
        }
    }

    #[cfg(target_os = "macos")]
    {
        let script = build_shell_script(working_dir, env, command, args);

        Command::new("osascript")
            .arg("-e")
            .arg(format!(
                "tell application \"Terminal\" to do script \"{}\"",
                script.replace('"', "\\\"")
            ))
            .arg("-e")
            .arg("tell application \"Terminal\" to activate")
            .spawn()
            .map_err(|e| format!("Failed to launch Terminal: {}", e))?;
    }

    #[cfg(target_os = "linux")]
    {
        let script = format!(
            "{}; exec bash",
            build_shell_script(working_dir, env, command, args)
        );
        let terminals = ["gnome-terminal", "konsole", "xfce4-terminal", "xterm"];

        for term in &terminals {
            let available = Command::new("which")
                .arg(term)
                .output()
                .map(|output| output.status.success())
                .unwrap_or(false);

            if available {
                Command::new(term)
                    .arg("--")
                    .arg("bash")
                    .arg("-c")
                    .arg(&script)
                    .spawn()
                    .map_err(|e| format!("Failed to launch terminal: {}", e))?;
                return Ok(());
            }
        }

        return Err("No suitable terminal emulator found".to_string());
    }

    Ok(())
}

#[cfg(target_os = "windows")]
fn build_cmd_line(command: &str, args: &[String]) -> String {
    std::iter::once(command.to_string())
        .chain(args.iter().map(|arg| quote_cmd_arg(arg)))
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(target_os = "windows")]
fn quote_cmd_arg(value: &str) -> String {
    if value
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.' | '/' | ':' | '='))
    {
        value.to_string()
    } else {
        format!("\"{}\"", escape_cmd_value(value))
    }
}

#[cfg(target_os = "windows")]
fn escape_cmd_value(value: &str) -> String {
    value.replace('"', "\\\"")
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
fn build_shell_script(
    working_dir: &str,
    env: &[(&str, &str)],
    command: &str,
    args: &[String],
) -> String {
    let exports = env
        .iter()
        .map(|(key, value)| format!("export {}={}", key, sh_quote(value)))
        .collect::<Vec<_>>()
        .join(" && ");
    let command_part = std::iter::once(command.to_string())
        .chain(args.iter().map(|arg| sh_quote(arg)))
        .collect::<Vec<_>>()
        .join(" ");

    if exports.is_empty() {
        format!("cd {} && {}", sh_quote(working_dir), command_part)
    } else {
        format!(
            "cd {} && {} && {}",
            sh_quote(working_dir),
            exports,
            command_part
        )
    }
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
fn sh_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

#[cfg(all(test, windows))]
mod tests {
    use super::build_cmd_line;

    #[test]
    fn windows_command_line_contains_only_command_and_arguments() {
        let command_line =
            build_cmd_line("claude", &["--model".to_string(), "test model".to_string()]);

        assert_eq!(command_line, "claude --model \"test model\"");
        assert!(!command_line.contains("TOKEN"));
        assert!(!command_line.contains("set \""));
    }
}
