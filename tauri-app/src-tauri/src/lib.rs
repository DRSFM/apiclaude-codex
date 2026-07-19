mod commands;
mod config;
mod secure_store;
mod stats;

use commands::{
    add_claude_node, add_codex_profile, get_claude_nodes, get_codex_profiles, remove_claude_node,
    remove_codex_profile, set_current_claude_node, start_claude, start_codex,
};
use stats::{
    check_stats_available, get_all_nodes_usage, get_node_usage_stats, get_usage_overview,
};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            get_claude_nodes,
            add_claude_node,
            remove_claude_node,
            set_current_claude_node,
            start_claude,
            get_codex_profiles,
            add_codex_profile,
            remove_codex_profile,
            start_codex,
            check_stats_available,
            get_node_usage_stats,
            get_all_nodes_usage,
            get_usage_overview,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
