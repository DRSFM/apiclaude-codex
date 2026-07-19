use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

pub const DEFAULT_CODEX_BASE_URL: &str = "https://api.openai.com/v1";
pub const DEFAULT_CODEX_MODEL: &str = "gpt-5.5";
const HIDDEN_PREFIX_CHARS: &[char] = &[
    '\u{feff}', '\u{200b}', '\u{200c}', '\u{200d}', '\u{2060}', '\u{fffd}',
];

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ClaudeNode {
    #[serde(default)]
    pub base_url: String,
    #[serde(default)]
    pub token: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ClaudeConfig {
    pub nodes: BTreeMap<String, ClaudeNode>,
    pub current: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodexProfile {
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub name: String,
    #[serde(rename = "baseUrl", default)]
    pub base_url: String,
    #[serde(default)]
    pub home: String,
    #[serde(rename = "createdAt", default)]
    pub created_at: String,
    #[serde(
        rename = "lastUsedAt",
        default,
        skip_serializing_if = "Option::is_none"
    )]
    pub last_used_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(default, skip_serializing)]
    pub api_key: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodexConfig {
    #[serde(default = "default_codex_version")]
    pub version: u32,
    #[serde(default)]
    pub profiles: Vec<CodexProfile>,
}

fn default_codex_version() -> u32 {
    1
}

pub fn now_iso() -> String {
    Utc::now().to_rfc3339()
}

pub fn clean_hidden_prefix(value: &str) -> String {
    value
        .trim_start_matches(HIDDEN_PREFIX_CHARS)
        .trim()
        .to_string()
}

pub fn slugify(value: &str) -> String {
    let mut out = String::new();
    let mut last_was_dash = false;

    for ch in value.trim().to_lowercase().chars() {
        let keep = ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-');
        if keep {
            out.push(ch);
            last_was_dash = false;
        } else if !last_was_dash {
            out.push('-');
            last_was_dash = true;
        }
    }

    let slug = out.trim_matches('-').to_string();
    if slug.is_empty() {
        "profile".to_string()
    } else {
        slug
    }
}

pub fn toml_basic_string(value: &str) -> String {
    format!("\"{}\"", value.replace('\\', "\\\\").replace('"', "\\\""))
}

pub fn get_claude_config_path() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".apiclaude_config.json")
}

pub fn get_codex_home() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".codex-api")
}

pub fn get_codex_profiles_path() -> PathBuf {
    get_codex_home().join("profiles.json")
}

pub fn get_codex_archive_root() -> PathBuf {
    get_codex_home().join("archived-profiles")
}

pub fn codex_profile_home(profile: &CodexProfile) -> PathBuf {
    if profile.home == "." {
        get_codex_home()
    } else {
        get_codex_home().join(&profile.home)
    }
}

pub fn load_claude_config() -> Result<ClaudeConfig, String> {
    let path = get_claude_config_path();
    if !path.exists() {
        return Ok(ClaudeConfig::default());
    }

    let content =
        fs::read_to_string(&path).map_err(|e| format!("Failed to read Claude config: {}", e))?;
    let value: Value = serde_json::from_str(content.trim_start_matches('\u{feff}'))
        .map_err(|e| format!("Failed to parse Claude config: {}", e))?;

    let mut config = ClaudeConfig {
        current: value
            .get("current")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned),
        ..ClaudeConfig::default()
    };

    match value.get("nodes") {
        Some(Value::Object(nodes)) => {
            for (name, node) in nodes {
                let base_url = node
                    .get("base_url")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                let token = node
                    .get("token")
                    .or_else(|| node.get("api_key"))
                    .and_then(Value::as_str)
                    .unwrap_or_default();

                config.nodes.insert(
                    name.clone(),
                    ClaudeNode {
                        base_url: clean_hidden_prefix(base_url),
                        token: clean_hidden_prefix(token),
                    },
                );
            }
        }
        Some(Value::Array(nodes)) => {
            for node in nodes {
                let Some(name) = node.get("name").and_then(Value::as_str) else {
                    continue;
                };
                if name.trim().is_empty() {
                    continue;
                }

                let base_url = node
                    .get("base_url")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                let token = node
                    .get("token")
                    .or_else(|| node.get("api_key"))
                    .and_then(Value::as_str)
                    .unwrap_or_default();

                if config.current.is_none()
                    && node
                        .get("current")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                {
                    config.current = Some(name.to_string());
                }

                config.nodes.insert(
                    name.to_string(),
                    ClaudeNode {
                        base_url: clean_hidden_prefix(base_url),
                        token: clean_hidden_prefix(token),
                    },
                );
            }
        }
        _ => {}
    }

    if let Some(current) = &config.current {
        if !config.nodes.contains_key(current) {
            config.current = None;
        }
    }

    Ok(config)
}

pub fn save_claude_config(config: &ClaudeConfig) -> Result<(), String> {
    let path = get_claude_config_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("Failed to create Claude config directory: {}", e))?;
    }

    let content = serde_json::to_string_pretty(config)
        .map_err(|e| format!("Failed to serialize Claude config: {}", e))?;

    fs::write(&path, content).map_err(|e| format!("Failed to write Claude config: {}", e))?;

    Ok(())
}

pub fn load_codex_config() -> Result<CodexConfig, String> {
    initialize_codex_store()?;

    let path = get_codex_profiles_path();
    if !path.exists() {
        return Ok(CodexConfig {
            version: 1,
            profiles: vec![],
        });
    }

    let content =
        fs::read_to_string(&path).map_err(|e| format!("Failed to read Codex profiles: {}", e))?;

    let mut config: CodexConfig = serde_json::from_str(content.trim_start_matches('\u{feff}'))
        .map_err(|e| format!("Failed to parse Codex profiles: {}", e))?;

    normalize_codex_profiles(&mut config);
    Ok(config)
}

pub fn save_codex_config(config: &CodexConfig) -> Result<(), String> {
    let path = get_codex_profiles_path();

    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("Failed to create Codex directory: {}", e))?;
    }

    let content = serde_json::to_string_pretty(config)
        .map_err(|e| format!("Failed to serialize Codex profiles: {}", e))?;

    fs::write(&path, content).map_err(|e| format!("Failed to write Codex profiles: {}", e))?;

    Ok(())
}

pub fn write_codex_config(home: &Path, base_url: &str, model: &str) -> Result<(), String> {
    fs::create_dir_all(home)
        .map_err(|e| format!("Failed to create Codex profile directory: {}", e))?;

    let content = format!(
        "model = {}\nmodel_provider = \"apicodex\"\nmodel_reasoning_effort = \"high\"\nauth_credentials_store = \"file\"\n\n[windows]\nsandbox = \"unelevated\"\n\n[model_providers.apicodex]\nname = \"API Codex\"\nbase_url = {}\nwire_api = \"responses\"\nrequires_openai_auth = true\n",
        toml_basic_string(model),
        toml_basic_string(base_url),
    );

    fs::write(home.join("config.toml"), content)
        .map_err(|e| format!("Failed to write Codex config.toml: {}", e))?;

    Ok(())
}

pub fn write_codex_auth(home: &Path, api_key: &str) -> Result<(), String> {
    fs::create_dir_all(home)
        .map_err(|e| format!("Failed to create Codex profile directory: {}", e))?;

    let content = serde_json::to_string_pretty(&serde_json::json!({
        "OPENAI_API_KEY": clean_hidden_prefix(api_key),
    }))
    .map_err(|e| format!("Failed to serialize Codex auth: {}", e))?;

    fs::write(home.join("auth.json"), content)
        .map_err(|e| format!("Failed to write Codex auth.json: {}", e))?;

    Ok(())
}

pub fn add_codex_project_trust(home: &Path, working_dir: &str) -> Result<(), String> {
    let config_path = home.join("config.toml");
    if !config_path.exists() {
        return Ok(());
    }

    let header = format!("[projects.{}]", toml_basic_string(working_dir));
    let raw = fs::read_to_string(&config_path)
        .map_err(|e| format!("Failed to read Codex config.toml: {}", e))?;

    if !raw.contains(&header) {
        let addition = format!("\n{}\ntrust_level = \"trusted\"\n", header);
        fs::OpenOptions::new()
            .append(true)
            .open(&config_path)
            .and_then(|mut file| {
                use std::io::Write;
                file.write_all(addition.as_bytes())
            })
            .map_err(|e| format!("Failed to update Codex project trust: {}", e))?;
    }

    Ok(())
}

pub fn mask_secret(value: &str) -> String {
    let head = 8;
    let tail = 5;
    let chars: Vec<char> = value.chars().collect();

    if value.is_empty() {
        return "<empty>".to_string();
    }

    if chars.len() <= head + tail {
        return "*".repeat(chars.len());
    }

    let prefix: String = chars[..head].iter().collect();
    let suffix: String = chars[chars.len() - tail..].iter().collect();
    format!("{}***{}", prefix, suffix)
}

fn initialize_codex_store() -> Result<(), String> {
    let codex_home = get_codex_home();
    fs::create_dir_all(&codex_home).map_err(|e| format!("Failed to create Codex home: {}", e))?;

    let profiles_path = get_codex_profiles_path();
    if profiles_path.exists() {
        return Ok(());
    }

    let mut config = CodexConfig {
        version: 1,
        profiles: vec![],
    };

    if codex_home.join("config.toml").exists() && codex_home.join("auth.json").exists() {
        let now = now_iso();
        config.profiles.push(CodexProfile {
            id: "default".to_string(),
            name: "default".to_string(),
            base_url: extract_base_url_from_config(&codex_home),
            home: ".".to_string(),
            created_at: now.clone(),
            last_used_at: Some(now),
            model: None,
            api_key: None,
        });
    }

    save_codex_config(&config)
}

fn normalize_codex_profiles(config: &mut CodexConfig) {
    for profile in &mut config.profiles {
        if profile.id.trim().is_empty() {
            profile.id = slugify(&profile.name);
        }
        if profile.name.trim().is_empty() {
            profile.name = profile.id.clone();
        }
        if profile.base_url.trim().is_empty() {
            profile.base_url = DEFAULT_CODEX_BASE_URL.to_string();
        }
        if profile.home.trim().is_empty() {
            profile.home = if profile.id == "default" {
                ".".to_string()
            } else {
                format!("profiles/{}", profile.id)
            };
        }
        if profile.created_at.trim().is_empty() {
            profile.created_at = now_iso();
        }
    }
}

fn extract_base_url_from_config(home: &Path) -> String {
    let config_path = home.join("config.toml");
    let Ok(content) = fs::read_to_string(config_path) else {
        return DEFAULT_CODEX_BASE_URL.to_string();
    };

    for line in content.lines() {
        let trimmed = line.trim();
        if !trimmed.starts_with("base_url") {
            continue;
        }
        let Some((_, value)) = trimmed.split_once('=') else {
            continue;
        };
        let parsed = value.trim().trim_matches('"').to_string();
        if !parsed.is_empty() {
            return parsed;
        }
    }

    DEFAULT_CODEX_BASE_URL.to_string()
}
