# apiagent / apicodex / apiclaude

Cross-platform API profile launchers for Codex CLI and Claude Code.

- `apicodex` manages Codex API profiles under `~/.codex-api`.
- `apiclaude` manages Claude Code API nodes in `~/.apiclaude_config.json`.
- `apiagent` is a shared entrypoint for both.

On Windows, API keys and tokens are encrypted with DPAPI under
`~/.apiagent-secrets`. The JSON/TOML config files contain only profile metadata
and credential references.

## Requirements

- Python 3
- Codex CLI available as `codex` for `apicodex`
- Claude Code CLI available as `claude` for `apiclaude`

Check:

```bash
python3 --version
codex --version
claude --version
```

On Windows:

```powershell
python --version
codex --version
claude --version
```

## Install On macOS Or Linux

Clone the repo, then run:

```bash
chmod +x install.sh
./install.sh
```

After that, use the commands directly:

```bash
apicodex
apiclaude
apiagent list
```

If the installer says `~/.local/bin` is not in PATH, add:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

## Install On Windows

Put this repository somewhere stable, then put these `.bat` files in a folder on
PATH, or add the repository folder to PATH:

```powershell
apiagent.bat
apicodex.bat
apiclaude.bat
```

## Codex Usage

Add or update a Codex API profile:

```bash
apicodex --api-add
```

List profiles:

```bash
apicodex --api-list
```

For GUI integrations, request the stable machine-readable contract:

```bash
apicodex --api-list --json
```

The JSON envelope has `schemaVersion` and `profiles`. Each profile exposes only
non-sensitive metadata: `id`, `instanceId`, `name`, `baseUrl`, `profileHome`,
`desktopData`, and `lastUsedAt`. It never includes API keys, tokens, cookies,
`auth.json` contents, or keyring values. Consumers should reject unsupported
schema versions and treat profile IDs as opaque validated identifiers.

Choose a profile and start Codex:

```bash
apicodex
```

Choose a profile and open the current folder in an isolated VS Code instance:

```bash
apicodex --vscode
```

Open VS Code with a specific profile without prompting:

```bash
apicodex --vscode --api-profile muyuanpub
```

Each profile uses a separate VS Code user-data directory under
`~/.apicodex-vscode`. The Codex extension inherits that profile's `CODEX_HOME`
and API key without placing the key on the command line.

Open the official ChatGPT desktop app with an isolated Codex API profile:

```bash
apicodex --desktop
apicodex --desktop --api-profile muyuanpub
```

Desktop profiles use separate browser/app data under
`~/.apicodex-desktop/<profile>` and the same profile-scoped `CODEX_HOME` used by
the CLI and VS Code extension. The launcher does not read or modify the normal
ChatGPT account-backed Codex home at `~/.codex`. The API key is passed only in
the child process environment or login stdin and is not placed on the command
line. Desktop launch is currently supported on Windows with the official
ChatGPT app.
The launcher also keeps the API desktop in Codex coding mode, so the project
menu includes local folders instead of falling back to ChatGPT cloud projects.
The API profile's master key remains DPAPI-encrypted by this launcher. When the
desktop starts, it is synchronized through stdin into the official Codex
Windows keyring for that isolated `CODEX_HOME`; no API key is placed on the
command line or written to plaintext `auth.json`.

### Repair Missing Desktop History Images

Codex session JSONL records can retain an attached image as an embedded data
URL while the adjacent history marker still points at a short-lived
`codex-clipboard-*` file under the Windows Temp directory. If that Temp file is
removed during a restart or cleanup, Desktop can keep showing a spinner even
though the image bytes still exist in the session. Tools that render the
embedded data URL directly are not affected by the missing Temp file.

`apicodex --desktop` now checks the selected API profile before Desktop starts
and reconstructs missing, validated clipboard files. A repair failure is
reported but does not block Desktop startup. The same operation can be run
explicitly:

```powershell
# Inspect or repair one selected API profile.
apicodex --repair-images --dry-run
apicodex --repair-images --api-profile muyuanpub

# Inspect or repair every API profile. This never includes the account home.
apicodex --repair-images --all --dry-run
apicodex --repair-images --all
```

The normal account-backed Codex home is separate and always requires the
explicit `--account` flag:

```powershell
apicodex --repair-images --account --dry-run
apicodex --repair-images --account
```

On Windows, account repair at sign-in is available as a reversible opt-in. No
task is installed automatically:

```powershell
apicodex --repair-images --account --install-task
apicodex --repair-images --account --uninstall-task
```

The repair engine reads only `<CODEX_HOME>/sessions`. It does not edit session
JSONL, `auth.json`, `config.toml`, SQLite files, keyring data, or Desktop user
data. Its index contains locations and hashes only and is stored under
`%LOCALAPPDATA%\apicodex\history-images`. Restored files must be direct children
of the current Temp directory, use a `codex-clipboard-UUID` image name, and
pass MIME, extension, size, structure, and SHA-256 checks. Existing files with
different contents are preserved and reported as conflicts.

This is a launcher-side compatibility repair based on the locally verified
session schema, not a promise about an official app-server storage contract.
If a future Codex release changes that schema, unrecognized records are left
untouched. After account repair, reopen the affected task if Desktop had
already loaded its missing-image state.

### Local Conversation Sharing Pool

`apicodex share` provides a Git-like local pool for continuing selected Codex
conversations in another account or API Profile. The default pool is
`E:\CodexConversationPool`. It is not a `CODEX_HOME`: it contains only
portable, content-addressed snapshots and version metadata.

On Windows the pool requires both EFS and a protected ACL that grants access
only to the current user, SYSTEM, and Administrators. Initialization stops if
either control cannot be enabled or verified; there is no plaintext fallback.
Check the operation first, then initialize it:

```powershell
apicodex share init --dry-run
apicodex share init
```

Back up the current Windows user's EFS certificate and private key after
initialization. Use `--pool E:\AnotherSecurePool` on any command to override
the configured location.

Publish a completed conversation as the first `main` version:

```powershell
# Choose the Profile and conversation interactively.
apicodex share publish antenna-notes

# Or select them explicitly.
apicodex share publish antenna-notes --api-profile relay --thread <THREAD_ID>
apicodex share publish account-task --account --thread <THREAD_ID>
```

Clone it into a target Profile. The target app-server creates a new local
thread ID, loads that Profile's current configuration, and names the task with
`[shared]` by default. Before forking, ApiCodex builds a temporary target
runtime copy whose `model`, provider, and working directory come from the
target Profile; those settings are audited again in the generated rollout so
portable placeholders cannot be sent to the upstream API:

```powershell
apicodex share clone antenna-notes --api-profile another-profile
apicodex share clone antenna-notes --commit <COMMIT_PREFIX> --account
apicodex share clone antenna-notes --ref main --cwd D:\work\antenna
```

Mapped source and cloned tasks behave like independent working copies:

```powershell
apicodex share status --api-profile another-profile --thread <THREAD_ID>
apicodex share push --api-profile another-profile --thread <THREAD_ID>

# If main moved, a normal push is rejected. Preserve the work explicitly:
apicodex share push --api-profile another-profile --thread <THREAD_ID> `
  --new-branch experiment
```

Inspect the pool and local compatibility:

```powershell
apicodex share list
apicodex share log antenna-notes
apicodex share doctor --api-profile another-profile
```

`--json` is available for machine-readable output. Mutating commands support
`--dry-run`; it validates and reports without changing pool refs, objects,
threads, or mappings.

Snapshots preserve visible user/assistant messages, tool calls and results,
image references, and compaction summaries in their original order. They
remove hidden reasoning, `encrypted_content`, credentials, token statistics,
old permissions/sandbox/Profile settings, and injected Skill/plugin/AGENTS
context. Unknown response-item types, active or half-written turns, source
changes during capture, object hash failures, and non-fast-forward pushes are
rejected instead of being silently degraded.

Version 1 is local and manually synchronized. It does not provide in-place
pull, automatic merge, background sync, repository copies, external tool-state
copies, deletion/GC, or cross-machine transport. `thread/fork.path` is an
experimental Codex capability; `share doctor` disables cloning safely if the
installed Codex no longer exposes it. The implementation never falls back to
editing Codex SQLite databases or target rollout JSONL directly.

For an opt-in Dream Skin instance, set `APICODEX_DREAM_SKIN_SCRIPT` to the
skin launcher's PowerShell path and `APICODEX_DREAM_SKIN_PORT` to a dedicated
loopback port before running `apicodex --desktop --api-profile <profile>`.
The launcher then passes the profile-scoped `CODEX_HOME`, API key, and Desktop
data directory to the skin entry point; the key is not placed in its arguments.
The Dream Skin path waits for its own startup verification, while the default
Desktop path remains detached.

After a successful API Desktop launch, ApiCodex labels the verified main window
as `ChatGPT (Profile name)`. The account-backed Desktop remains `ChatGPT`.
Labeling matches the official executable and exact isolated Desktop data path;
failure only produces a warning and never blocks launch. The integrated Dream
Skin WPF launcher provides the unified tray menu and retains all existing skin,
profile, and instance controls.

Run a specific profile:

```bash
apicodex --api-profile bohe resume
```

Other management commands:

```bash
apicodex --api-remove
apicodex --up
apicodex --api-help
```

`apicodex --up` runs the official Codex installer to update the standalone
Codex CLI. On Windows, PowerShell (`pwsh` or `powershell`) must be available.

## Claude Usage

Add or update a Claude API node:

```bash
apiclaude add
```

New nodes default to an isolated per-node config directory: Claude Code runs
with `CLAUDE_CONFIG_DIR` pointing at `~/.apiclaude/nodes/<slug>`, so sessions,
project history, and settings do not mix between nodes or with the normal
account state in `~/.claude`. Nodes saved by older versions keep the legacy
shared behavior until switched.

Show or switch a node's mode at any time:

```bash
apiclaude mode NAME            # show current mode
apiclaude mode NAME isolated   # node-scoped CLAUDE_CONFIG_DIR
apiclaude mode NAME shared     # default ~/.claude (legacy behavior)
```

Switching modes only changes which config directory is used on the next
launch; nothing is moved or deleted. A node switched to isolated for the first
time starts with a fresh directory (Claude Code will re-run onboarding and
trust prompts there), while existing history stays in `~/.claude`. Switching
back to shared leaves the isolated directory in place for later use. Removing
a node archives its isolated directory under `~/.apiclaude/archived-nodes`.

The isolated directory follows the node name, not the base URL or token, so
editing a node's credentials — or changing the upstream behind a local proxy —
never affects its local workspace.

Choose a node and start Claude Code:

```bash
apiclaude
```

Pass Claude Code arguments after `apiclaude`:

```bash
apiclaude --permission-mode bypassPermissions
apiclaude resume
apiclaude -c
```

Other management commands:

```bash
apiclaude list
apiclaude current
apiclaude remove NAME
apiclaude help
```

Run Claude Code with the current node without selecting again:

```bash
apiclaude run --version
```

## Shared Entry

`apiagent` forwards to either tool:

```bash
apiagent list
apiagent codex --api-list
apiagent codex --api-profile bohe resume
apiagent claude add
apiagent claude resume
```

## Hidden Character Guard

Both Codex API keys and Claude tokens are cleaned for common invisible prefix
characters such as UTF-8 BOM (`U+FEFF`) and zero-width characters before they are
saved or passed to the underlying CLI.

## Credential Storage

- Encryption is bound to the current Windows user through DPAPI. No master
  password is required.
- Existing plaintext Claude `token` fields and Codex `auth.json` API keys are
  migrated on the first `apiclaude` or `apicodex` load.
- Migration writes and reads back the encrypted value before removing plaintext
  from configuration files.
- Normal account login state under `~/.codex` and `~/.claude` is not changed.
- `apicodex` disables ChatGPT-hosted apps/plugins for API profiles so the CLI
  does not attempt unavailable `codex_apps` host authentication. This does not
  affect ordinary `codex` account sessions.
- Secure credential storage is currently supported on Windows. macOS and Linux
  secure backends are not implemented yet.
