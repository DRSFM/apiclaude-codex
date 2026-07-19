use crate::secure_store::{get_secret, set_secret};
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
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub token: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub credential_id: Option<String>,
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
    #[serde(
        rename = "credentialId",
        default,
        skip_serializing_if = "Option::is_none"
    )]
    pub credential_id: Option<String>,
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
                let credential_id = node
                    .get("credential_id")
                    .and_then(Value::as_str)
                    .map(ToOwned::to_owned);

                config.nodes.insert(
                    name.clone(),
                    ClaudeNode {
                        base_url: clean_hidden_prefix(base_url),
                        token: clean_hidden_prefix(token),
                        credential_id,
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
                let credential_id = node
                    .get("credential_id")
                    .and_then(Value::as_str)
                    .map(ToOwned::to_owned);

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
                        credential_id,
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

    let mut migrated = false;
    for (name, node) in &mut config.nodes {
        if node.token.is_empty() {
            continue;
        }
        let credential_id = node
            .credential_id
            .clone()
            .unwrap_or_else(|| format!("claude:{}", name));
        set_secret(&credential_id, &node.token)?;
        if get_secret(&credential_id)? != node.token {
            return Err(format!(
                "Failed to verify migrated credential '{}'",
                credential_id
            ));
        }
        node.credential_id = Some(credential_id);
        node.token.clear();
        migrated = true;
    }
    if migrated {
        save_claude_config(&config)?;
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
    let mut migrated = false;
    let mut pending_sanitization = Vec::new();
    for profile in &mut config.profiles {
        let credential_id = profile
            .credential_id
            .clone()
            .unwrap_or_else(|| format!("codex:{}", profile.id));
        let home = if profile.home == "." {
            get_codex_home()
        } else {
            get_codex_home().join(&profile.home)
        };
        let auth_path = home.join("auth.json");
        let saved_key = read_codex_api_key(&auth_path);
        let embedded_key = profile.api_key.clone().unwrap_or_default();
        let secret = if embedded_key.trim().is_empty() {
            saved_key.clone()
        } else {
            clean_hidden_prefix(&embedded_key)
        };

        if !secret.is_empty() {
            set_secret(&credential_id, &secret)?;
            if get_secret(&credential_id)? != secret {
                return Err(format!(
                    "Failed to verify migrated credential '{}'",
                    credential_id
                ));
            }
            profile.credential_id = Some(credential_id.clone());
            profile.api_key = None;
            write_codex_config(
                &home,
                &profile.base_url,
                profile.model.as_deref().unwrap_or(DEFAULT_CODEX_MODEL),
            )?;
            if !saved_key.is_empty() {
                pending_sanitization.push((auth_path, credential_id.clone(), saved_key));
            }
            migrated = true;
        }
    }
    if migrated {
        save_codex_config(&config)?;
        for (auth_path, credential_id, saved_key) in pending_sanitization {
            if get_secret(&credential_id)? != saved_key {
                return Err(format!(
                    "Refusing to sanitize unverified credential '{}'",
                    credential_id
                ));
            }
            let sanitized = serde_json::to_string_pretty(&serde_json::json!({
                "credential_store": "windows-dpapi",
                "credential_id": credential_id,
            }))
            .map_err(|error| format!("Failed to sanitize Codex auth: {}", error))?;
            fs::write(&auth_path, sanitized)
                .map_err(|error| format!("Failed to sanitize Codex auth.json: {}", error))?;
        }
    }
    Ok(config)
}

fn read_codex_api_key(path: &Path) -> String {
    let Ok(content) = fs::read_to_string(path) else {
        return String::new();
    };
    let Ok(value) = serde_json::from_str::<Value>(content.trim_start_matches('\u{feff}')) else {
        return String::new();
    };
    value
        .get("OPENAI_API_KEY")
        .and_then(Value::as_str)
        .map(clean_hidden_prefix)
        .unwrap_or_default()
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
        "model = {}\nmodel_provider = \"apicodex\"\nmodel_reasoning_effort = \"high\"\n\n[windows]\nsandbox = \"unelevated\"\n\n[shell_environment_policy]\nexclude = [\"APICODEX_API_KEY\"]\n\n[features]\napps = false\nplugins = false\n\n[model_providers.apicodex]\nname = \"API Codex\"\nbase_url = {}\nwire_api = \"responses\"\nenv_key = \"APICODEX_API_KEY\"\nrequires_openai_auth = false\n",
        toml_basic_string(model),
        toml_basic_string(base_url),
    );

    fs::write(home.join("config.toml"), content)
        .map_err(|e| format!("Failed to write Codex config.toml: {}", e))?;

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
            credential_id: None,
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
