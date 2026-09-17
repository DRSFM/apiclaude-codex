# apiagent / apicodex / apiclaude

Cross-platform API profile launchers for Codex CLI and Claude Code.

- `apicodex` manages Codex API and named ChatGPT profiles under `~/.codex-api`.
- `apiclaude` manages Claude Code API nodes in `~/.apiclaude_config.json`.
- `apiagent` is a shared entrypoint for both.

The Python CLI launchers protect API keys and tokens with Windows DPAPI or the
macOS login Keychain. Their JSON/TOML config files contain only profile metadata
and credential references.

## Requirements

- Python 3
- Codex CLI available as `codex` for `apicodex`
- Claude Code CLI available as `claude` for `apiclaude`
- CLIProxyAPI (CPA) v7.2.101 or newer for new Codex-to-Claude bridge nodes
- Optional: LiteLLM 1.93.0 or newer for legacy bridge nodes created before CPA
- Optional: `cryptography` for Windows OAuth JSON import only (`python -m pip install cryptography`)

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

## Named ChatGPT subscription accounts

Running `apicodex` always offers `[0] Account login`, followed by numbered API
profiles. Account login opens a second menu: `[1] Official login`, named accounts,
and `[a] Add profile`. The official entry launches ordinary Codex with `~/.codex`;
it reuses the existing login and lets Codex handle authentication when needed.
The same selection works with `apicodex --desktop`.

Add profile offers browser login, device login, or an explicitly selected OAuth
JSON file. It suggests the account email as the profile name; accept it or enter
a short alias. Codex's account response exposes an email, not a separate public
OpenAI username. Directory IDs stay stable when names change, and previous names
remain usable as aliases:

```powershell
apicodex account add
apicodex account import --file D:\private\account.json --dry-run
apicodex account import --file D:\private\account.json
apicodex account rename user@example.com short-name
apicodex --account-profile short-name
apicodex --desktop --account-profile short-name
```

Run ordinary `codex` to keep using the default official account. A named ChatGPT
profile uses a separate `CODEX_HOME` under `~/.codex-api/accounts/<stable-id>`;
its official Desktop window also uses a separate `~/.apicodex-desktop/<stable-id>`.
The default account's credentials are never automatically copied or changed.
This feature is managed through the CLI. The old Web/Tauri managers are not
supported for subscription accounts; Tauri refuses a registry containing them
before it can discard unfamiliar fields.

```powershell
apicodex account add planning --model gpt-6-astra
apicodex account add execution --model gpt-5.6-sol
apicodex account login planning
apicodex account login execution
apicodex account list
apicodex --account-profile planning
apicodex --account-profile execution --model gpt-5.6-sol
apicodex --desktop --account-profile execution
```

`add NAME` creates metadata/configuration only; `add` without a name opens the
guided login/import flow. Normal CLI/Desktop launches reuse
the selected profile's stored authentication and let official Codex refresh it;
they do not call login, inject API keys, or query an API provider's model list.
The explicit `account login` command first asks official Codex to read/refresh
cached authentication and opens the official login flow only when needed.
For device authorization use `account login NAME --device-auth` (subject to
account/workspace support). Temporary network errors do not clear stored auth
or trigger a replacement login. Credentials use official `keyring` storage,
without a plaintext fallback. The account must itself have access to the model
you select; the example model names do not grant model access.

To import an explicitly selected official `auth.json` export or Cockpit Tools
OAuth export on Windows:

```powershell
apicodex account import execution --file D:\private\account.json --dry-run
apicodex account import execution --file D:\private\account.json
apicodex --desktop --account-profile execution
```

`import` creates a new profile; if you already used `account add`, pass `--update`
to explicitly import into that existing profile. It accepts the official nested
`tokens` object and Cockpit's flat token export (including single-entry arrays).
For a multi-account array, choose one with `--index N`, starting at 1. Preview
shows only masked identity, expiry, and whether a refresh token exists; decoded
claims do not prove identity or service access. Both `id_token` and `access_token`
are required. Without `refresh_token`, the imported login cannot renew itself.
Access-only, API-key, and agent-identity exports are rejected; use official
login for these subscription profiles instead.

Only import loads the optional `cryptography` package. It writes the official
age-encrypted `secrets/codex_auth.age` and stores its passphrase in the Windows
credential manager under the official path-derived identity. It verifies local
readback and official Codex recognition before recording success, preserving
unrelated secrets and backing up previous ciphertext for an explicit update.
Failed verification or registry persistence restores the prior auth. The source
file is never changed, registered as a startup source, or copied into the profile.
Subsequent launches use the official refreshed credentials.

Reimporting an identity already recorded by this importer reuses its existing
profile unless `--update` explicitly replaces that profile's login. Reuse does
not check or restore authentication: after logout, use `account login NAME`, or
explicitly import a current export into that existing profile with `--update`.
Accounts
created through browser login have no import identity marker; their existing
profile also requires explicit `--update`. This is not live synchronization with
Cockpit: refreshing the same copied account in both tools can invalidate an old
refresh token. Use one active credential owner for that account; separate
accounts remain independent. macOS supports official account login, while this
file-import adapter and Desktop launch currently support Windows only.

```powershell
apicodex account status execution --json
apicodex account status execution --refresh
apicodex account model execution gpt-6-astra
apicodex account logout execution --dry-run
apicodex account logout execution
apicodex account archive execution --dry-run
apicodex account archive execution
```

Status reports official local authentication recognition, not a successful
model request. Close that profile's CLI/Desktop sessions before logout or
archive. Logout clears only its official login. Archive retains its credentials
and history, moves its directories before unregistering it, and records the
original paths in `archived-profiles/<id>/profile.json`. It does not sign out
other accounts. To undo an archive, close its sessions, restore `home` and
`desktop` to the manifest's original paths and restore the manifest's `profile`
entry to `profiles.json`; the original path matters because official keyring
storage is tied to `CODEX_HOME`. Do not overwrite an existing profile or restore
while another profile-management operation is running.

### Shared resources for named accounts

Named accounts follow the official default account's resources automatically:

- `skills`, `rules`, `prompts`, and `plugins/cache` link to the same paths under
  `~/.codex`. Windows uses directory junctions without administrator privileges;
  other platforms use directory symlinks. Adding a Skill through a named
  account's `skills` directory therefore writes directly to the shared source.
  User-wide `~/.agents/skills` and personal `~/.agents/plugins` remain shared too.
- `AGENTS.md`, `AGENTS.override.md`, and `hooks.json` follow the default source
  by atomic copy on each launch, including source deletions. Edit those files
  in `~/.codex`. Hook review/trust state in each account's `config.toml` stays
  independent; a changed hook definition requires review again.
- MCP definitions, plugin/marketplace declarations, Skill enablement, feature
  flags, interface settings and common permission settings follow the default
  `config.toml` on launch. Account model choices, provider/authentication fields,
  trust/history and runtime data remain separate. A shared feature setting
  cannot disable named-account encrypted authentication.

For a named profile, `mcp add/remove` and plugin/marketplace management commands
update the default source, then synchronize that profile. Other accounts pick
up the change at their next launch. MCP OAuth login/logout and connected-service
authentication remain account-specific; sharing definitions does not share
service credentials or account entitlements. Already open sessions may require
restarting to read configuration changes.

```powershell
apicodex account sync --all --dry-run
apicodex account sync --all
apicodex account sync short-name
apicodex --account-profile short-name mcp add example -- example-mcp
```

Before replacing local resources, synchronization retains originals under the
named home's `.account-resource-backups/<id>`. Local-only user files are copied
and checked into the default resource source; default files win same-path
conflicts, with originals retained in the backup. Generated `.system` Skills
follow the default runtime. Unknown local nested links stay in the backup for
manual review. Dry-run previews directories and config changes; it does not
enumerate file conflicts or detect live file locks.

If an open Desktop locks `plugins/cache`, the existing cache stays in place and
the command reports deferred sharing; close that account's Desktop and relaunch
or run `account sync NAME` to retry. Other resource sharing still completes.
A failed resource step retains originals/backups; previously completed steps
are not rolled back as a global transaction. To recover a local variant, close
the account's sessions and copy the desired backed-up files into the shared
source. Do not recursively delete a junction or overwrite authentication data.
Neither credentials, histories, databases nor plugin runtime/staging directories
are linked to the default account.

### Optional two-line usage display

[Token Tracker](https://github.com/stormzhang/token-tracker) can display colored
5-hour/week quota bars, reset countdowns, model and context usage after each
completed reply. It uses the official Codex `Stop` hook; it does not replace
the native footer or require a custom Codex binary. Its bars show **used**
percentages, whereas the native footer normally shows percentages **left**.
The hook output uses Codex's warning-message UI channel, so a warning label on
this usage display does not mean the request failed. Account credit balance is
not included in this renderer.

The Windows installation verified on this machine pins Token Tracker 0.5.7 at
commit `85a6b573bb53e8181772abd5f590e0383f1bb28a` in a separate Python environment
under `C:\tools\token-tracker\venv`. Only its rendered Codex status hook is
registered; its general setup wizard, sidebar and Claude/Kimi integrations are
not run. The command in the default `~/.codex/hooks.json` is:

```text
C:/tools/token-tracker/venv/Scripts/python.exe -X utf8 C:/tools/token-tracker/codex-statusline.py
```

`features.hooks = true` enables execution. Named account launches synchronize
the definition while keeping each account's hook trust local. Newly created
accounts can review and trust this exact command via `/hooks`; existing local
accounts were reviewed and verified during installation. Restart an already
running CLI to load the hook, then complete one reply. The renderer follows
the session transcript and its `CODEX_HOME`; it does not read login credentials
or make requests to fetch quota. Until a session has quota data, some fields
can be absent; values are snapshots from completed requests.

Installation hashes and configuration backups are recorded in
`C:\tools\token-tracker\installation.json`. To disable the display, remove
only its command handler from the default `hooks.json`, then run
`apicodex account sync --all` and restart the CLI. Preserve any other hooks.
The package is optional and adds no dependency to ordinary ApiCodex startup.

Use the existing protected conversation pool for explicit handoff. Initialize
it once if necessary with `apicodex share init`, then list target IDs and copy
one selected conversation:

```powershell
apicodex share targets
apicodex share threads --target chatgpt:PROFILE_ID
apicodex share copy --from chatgpt:SOURCE_ID --to api:TARGET_ID --thread THREAD_ID --cwd D:\work
```

`share copy` reuses visible-history cleaning and creates an independent target
thread. It retains the source conversation and supports both directions between
API and named ChatGPT profiles. Existing `share publish`/`clone` commands also
accept `--account-profile NAME`. No automatic routing, account rotation, or
live thread synchronization is added. VS Code and CPA bridging are not supported
for subscription profiles.

Current validation: official CLI 0.154.0 recognizes synthetic OAuth credentials
larger than the Windows direct-credential limit in two isolated homes, including
concurrent reads, fresh-process reads, and independent logout. Officially
rewritten ciphertext is readable; temporary test keyring entries are removed.
Automated tests also cover import preview, format rejection, encrypted rollback,
launch routing, environment cleaning, and API/ChatGPT history handoff.
On this Windows machine, two explicitly supplied OAuth exports were imported;
both accounts answered real requests (Astra/Sol), rotated their access and
refresh tokens through official Codex, and answered again in fresh processes.
A real A-to-B history copy resumed successfully with Sol and retained the source
file's hash. Official Desktop 26.908.9136.0 opened both isolated windows at once:
one reached the main interface with Sol, while the other reached the official
first-use occupation/preferences screen after an initially blank window was
closed and reopened. The user subsequently confirmed entering Desktop;
full Desktop process-restart and per-account connector authorization acceptance
remain pending. Plugin-cache sharing may be deferred while Desktop holds files
open; a deferred result does not mean sharing has completed. No login page was
opened by the launcher, and default-account credentials were not read or written.
No installed launcher is
automatically updated: to review this checkout on Windows, replace `apicodex`
in the examples with `python .\apiagent.py codex`.

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

Add or update a Codex API profile with the guided setup:

```bash
apicodex --api-add
```

The setup asks for the profile name, the OpenAI-compatible API base URL, and the
API key. It then calls the provider's authenticated `GET /models` endpoint,
filters out obvious non-agent endpoints such as image and embedding models,
and displays a numbered model picker:

```text
Profile name: my-provider
API base URL [https://api.openai.com/v1]: https://gateway.example/v1
API key:
Available Codex-compatible models
[1] model-a
[2] model-b
Choose default model number or name: 2
```

The discovered text models are saved to the isolated profile's `models.json`,
so they remain available in Codex's later model picker. The chosen model becomes
that profile's default. The API key is never accepted as a command-line
argument; it is read through a hidden prompt, used in memory for discovery, and
stored in the platform secure store. Normal Codex arguments such as
`apicodex --model MODEL` continue to pass through to Codex.

### Refresh Codex model catalogs

CLI, VS Code, and Desktop launches automatically check the provider's model
list when a profile uses its local `models.json` override. Successful checks
are cached for six hours in `models-refresh.json`; changing the catalog or
provider URL invalidates that cache. API keys are reused from the secure store,
without a prompt. Help, version, and profile-list commands do not refresh.

To refresh immediately, or preview the additions without changing profile files:

```powershell
apicodex models refresh --api-profile zzzcoding
apicodex models refresh --all
apicodex models refresh --api-profile zzzcoding --dry-run
```

Refresh appends newly discovered text models and uses installed Codex metadata
for their reasoning levels, context limits, and capabilities where available.
Existing model entries, their custom metadata, the default model, and
`config.toml` remain unchanged. Models missing from a provider response are
retained, since gateways can return incomplete lists. This does not guarantee
that a retained model is still supported by the provider.

Before replacement, the original catalog is saved beside it as
`models.json.backup-<timestamp>-<suffix>`. Catalog writes are atomic, concurrent
refreshes are locked, and failed discovery preserves the current catalog and
does not prevent launch. Network requests use a five-second socket timeout;
the optional local metadata probe has a five-second process timeout. Failed
checks are retried on the next launch. The explicit command returns nonzero
if any selected profile fails.

Profiles using Codex's built-in catalog or an external catalog path are skipped.
To temporarily disable startup refresh in PowerShell, set
`$env:APICODEX_AUTO_REFRESH_MODELS = "0"`; an explicit `models refresh` still runs.
An already running Desktop or VS Code Codex session needs to be closed and
reopened to load an updated catalog. A listed model still requires working
access through the selected provider.

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
`desktopData`, `useCustomCodexCli`, and `lastUsedAt`. It never includes API keys,
tokens, cookies, `auth.json` contents, or keyring values. Consumers should reject
unsupported schema versions and treat profile IDs as opaque validated identifiers.

ApiCodex uses the official Codex CLI by default for every existing and newly
created Profile. To opt one Profile into the local custom build, run
`apicodex --cus` (or add `--api-profile NAME`) and answer `yes`. Pressing Enter
accepts the default `no` and switches the selected Profile back to the official
CLI.

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
the CLI and VS Code extension. By default, the launcher does not read or modify
the normal ChatGPT account-backed Codex home at `~/.codex`. The opt-in shared MCP
commands documented below read only its `config.toml`; they never copy account
authentication or conversation state. The API key is passed only in the child
process environment or login stdin and is not placed on the command line.
Desktop launch is currently supported on Windows with the official ChatGPT app.
The launcher also keeps the API desktop in Codex coding mode, so the project
menu includes local folders instead of falling back to ChatGPT cloud projects.
The API profile's master key remains DPAPI-encrypted by this launcher. When the
desktop starts, it is synchronized through stdin into the official Codex
Windows keyring for that isolated `CODEX_HOME`; no API key is placed on the
command line or written to plaintext `auth.json`.

### Shared Skills And MCP Servers

Codex discovers user-wide skills from `~/.agents/skills`. Put custom skills in
that directory when they should be available to both the account-backed Codex
home and every isolated ApiCodex profile. Do not install custom skills into a
profile's managed `.system` directory.

MCP configuration normally follows `CODEX_HOME`, so isolated API profiles do
not inherit `~/.codex/config.toml` automatically. Enable an explicit managed
copy from the account config:

```powershell
apicodex shared enable --account --dry-run
apicodex shared enable --account
apicodex shared status
apicodex shared sync
apicodex shared disable
```

After enablement, every CLI, VS Code, or Desktop launch refreshes all registered
API profiles from the account config. ApiCodex copies only `[mcp_servers.*]`
tables. Runtime-owned `node_repl`, `cua_repl`, and `apicodex_*` servers remain
profile-local. If a copied server is later edited inside one API profile,
ApiCodex reports a conflict and preserves that local version instead of
overwriting it. Changed files are backed up under
`~/.codex-api/shared-mcp-backups` before atomic replacement.

### Experimental Per-Profile Vision Fallback

A text-only model, or an upstream that blocks image input, can use Gemini as a
visual perception helper without changing other profiles. Configure one or
more existing profiles through the hidden API-key prompt:

```powershell
apicodex vision setup deepseek prism
apicodex vision status
apicodex vision disable deepseek prism
```

The adapter uses `gemini-3.5-flash-lite`. Setup validates the key with a small
image before changing any profile. For each selected Codex profile, a
loopback-only Responses proxy and a local `apicodex_vision` MCP server start
automatically. You can attach images directly in the Codex input box. The proxy
removes the raw image before forwarding, computes a SHA-256 image ID, and gives
the text-only main model an `inspect_images` tool. The main model decides
whether existing visual observations are sufficient and calls Gemini only when
more visual evidence is needed. A replayed history image alone never triggers
Gemini.

Visual observations are cached by the ordered image IDs, focused question,
Gemini model, and adapter prompt version. An identical inspection reuses the
local text cache without uploading the image to Gemini or calling Gemini. A
different focused question may require a new call. To deliberately bypass a matching
cache, explicitly ask the main model to inspect the original image again; the
tool exposes a `refresh` option for that user-directed case. Raw image bytes
remain only in the running worker's memory, while the persistent profile cache
contains Gemini's text observations, not copies of the images.

Every final answer from a vision-enabled Codex profile appends one of these
status lines, including turns where no image or vision tool is used:

- `视觉辅助：本轮已调用 Gemini`
- `视觉辅助：本轮未调用 Gemini（复用缓存）`
- `视觉辅助：本轮未调用 Gemini`

Claude bridges that reference the same Profile retain the earlier eager image
captioning path because they do not load the Codex Profile's MCP server.

Only the selected profiles are modified. The Gemini key and each proxy control
token are stored in the platform secure store; neither is placed in profile
JSON, TOML, process arguments, or logs. The proxy listens only on `127.0.0.1`,
requires the profile's existing bearer token before forwarding, and sends
Gemini requests with `store: false`. Images sent through an enabled profile are
therefore disclosed to Google for analysis, but prior conversation history is
not added to that Gemini request. The original image is not forwarded to the
text-only primary model.

This experimental path currently handles inline PNG, JPEG, WebP, HEIC, and HEIF
data URLs below Gemini's combined 20 MB request limit. No computer reboot is
required. Close and reopen an already-running Profile after an adapter update
so Codex reloads the local MCP tool and worker. Disabling the fallback restores
the original upstream URL, removes the Profile-owned MCP configuration, and
restores the text-only model catalog entry; the shared Gemini credential is
removed after the last enabled profile is disabled.

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
conversations in another account or API Profile. On first use the default pool
is `%USERPROFILE%\CodexConversationPool`; it is not tied to a particular drive.
It is not a `CODEX_HOME`: it contains only portable, content-addressed
snapshots and version metadata.

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

Clone it into a target Profile. Each copy gets a new local
thread ID, loads that Profile's current configuration, and names the task with
`[shared]` by default. Before forking, ApiCodex builds a temporary target
runtime copy whose `model`, provider, and working directory come from the
target Profile; those settings are audited again in the generated rollout so
portable placeholders cannot be sent to the upstream API:

History operations select the newest locally installed Desktop/official runtime,
independently of the ordinary CLI's per-Profile custom-build setting. Legacy
history uses `thread/fork.path`. Paginated history is materialized as a new,
exclusive standalone rollout with a fresh UUID, contiguous ordinals and no
`history_base` dependency, then indexed using `thread/resume` without starting a
model turn. This preserves modern `item_completed` records, including command
and image-view cards. Every completion payload and UI item identity is read back
and checked before a successful mapping is registered. `--dry-run` reports the
selected import mode; unsupported runtimes fail before creating an import.

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
installed Codex no longer exposes it. The implementation does not edit Codex
SQLite databases or overwrite existing conversations. Paginated imports create
only a newly named rollout; app-server builds its indexes. Older incomplete
copies and the original source remain available.

### Codex ↔ Claude Code conversation migration

The Web manager's **Conversation Migration** view also exposes every configured
Claude Code node alongside Account Codex and ApiCodex Profiles. It supports
Codex → Codex, Claude Code → Codex, Codex → Claude Code, and copies between
distinct Claude Code nodes. Every operation publishes the cleaned visible
history to the same protected local pool and creates a new target session ID;
the source transcript is never edited.

Claude Code → Codex converts visible user/assistant messages and user image
attachments into a portable Codex snapshot, then uses the target Codex
app-server to create and verify an independent thread. An interrupted Claude
turn can be recovered as a completed historical boundary so the new Codex task
can continue from the last visible prompt.

Codex → Claude Code materializes a new Claude transcript under the selected
node's shared or isolated `CLAUDE_CONFIG_DIR`. The generated transcript has a
new UUID, a validated parent chain, target cwd, target-node model metadata, and
a `custom-title`; the result includes the exact
`apiclaude --api-profile <node> --resume <session-id>` command. This path uses
Claude Code's local transcript and `--resume` contract because Claude Code does
not expose a Codex-style `thread/fork.path` API.

Cross-runtime copies preserve visible user/assistant history and user-provided
images. Runtime-specific hidden thinking, injected instructions, credentials,
usage data, and raw tool protocol records are removed. Tool effects already
present in the shared working directory remain available for the target agent
to inspect. Same-runtime Codex copies retain the richer supported Codex tool
lifecycle described above.

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

`apiclaude` accepts the same command style as `apicodex` — only the tool name
differs. The original subcommands (`add`, `list`, `current`, `remove`, `proxy`,
`run`, `vscode`, `update`, `help`) remain available as aliases.

Add or update a Claude API node:

```bash
apiclaude --api-add
```

New nodes use `http://127.0.0.1:7897` by default. The add prompt offers a
`[Y/n]` proxy choice, so pressing Enter keeps the proxy enabled. For normal
Anthropic nodes, the launcher sets both `HTTP_PROXY` and `HTTPS_PROXY` in the
Claude CLI or VS Code child environment. For Codex bridge nodes, the same
setting controls the upstream connection in the local authentication shim and
does not proxy the loopback Claude-to-CPA connection.

Choose a saved node and enable or disable its proxy at any time:

```bash
apiclaude --proxy
apiclaude --proxy --api-profile anyrouter
```

Disabling a proxy preserves its configured URL for the next enable action and
removes inherited `HTTP_PROXY` / `HTTPS_PROXY` values from the launched child.
Existing nodes without proxy settings are migrated once to the enabled default
when ApiClaude next loads its node registry.

New nodes default to an isolated per-node config directory: Claude Code runs
with `CLAUDE_CONFIG_DIR` pointing at `~/.apiclaude/nodes/<slug>`, so sessions,
project history, and settings do not mix between nodes or with the normal
account state in `~/.claude`. Nodes saved by older versions keep the legacy
shared behavior until switched.

Legacy shared mode also shares the account's saved `/model` choice and login
metadata. API authentication still takes precedence, but a node with
`cli_force_default_model: false` will follow model changes made in the account
CLI. Check `apiclaude mode NAME` before assuming an older node is isolated.
To separate an existing node while retaining history, copy and verify its
transcripts and related files before switching; do not copy account credentials
or the entire account configuration. Shared transcripts may lack provider tags,
so an exact historical split by API node is not always possible.

When an isolated Claude CLI is launched from the OS user home itself (for
example `C:\Users\SFM`), ApiClaude defaults to `--setting-sources user`. Otherwise
Claude also reads the account's `.claude/settings.json` as project settings,
which can override the isolated node's saved model. Other working directories
keep normal project/local settings. An explicit `--setting-sources` takes
precedence if you intentionally use the user home as a project.

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

### Shared Claude MCP Servers

Claude Code stores user-scoped MCP definitions in `~/.claude.json`. Shared-mode
ApiClaude nodes already read that file directly, while isolated nodes use their
own `.claude.json`. Enable managed synchronization so both modes receive the
same user MCP servers:

```powershell
apiclaude shared enable --account --dry-run
apiclaude shared enable --account
apiclaude shared status
apiclaude shared sync
apiclaude shared disable
```

The explicit `--account` flag authorizes the initial read of the account user
configuration. ApiClaude copies only `mcpServers`; account identity, sessions,
projects, cached state, and credentials outside MCP definitions are never
copied. Existing isolated CLI nodes are updated immediately, and future node,
VS Code, bridge, and Desktop launches refresh the managed copies automatically.
Locally edited definitions win on a name conflict. Changed files are backed up
under `~/.apiclaude/shared-mcp-backups` before atomic replacement.

This machine registers `SDW_Search` in both the ApiCodex account MCP source and
the Claude user MCP source. Its Codex definition is synchronized to every
ApiCodex profile, and its Claude definition is synchronized to every isolated
ApiClaude node.

### Experimental Codex Profile bridge

The CLI prototype can expose an existing `apicodex` API Profile to Claude Code
through a short-lived local Anthropic Messages-compatible bridge:

```bash
apiclaude bridge muyuan \
  --cpa-exe "F:/path/to/cli-proxy-api.exe" \
  --proxy-url "http://127.0.0.1:7897"
apiclaude --api-profile codex-muyuan
```

The first command creates an isolated Claude node that references the Codex
Profile; it does not copy its API key. At launch, `apiclaude` reads the key from
the existing platform secure credential store, starts CPA and a minimal in-memory
authentication shim bound only to `127.0.0.1`, and keeps them alive for the
Claude Code process. CPA performs the Anthropic Messages → OpenAI Responses
translation. The shim injects the upstream key without placing it in CPA's
temporary YAML configuration. The Profile's model is detected from its
`config.toml`. The node name, model, CPA executable, and optional upstream proxy
can be selected explicitly:

```bash
apiclaude bridge anyrouter --name gpt-shell --model gpt-5.6-sol \
  --cpa-exe "F:/path/to/cli-proxy-api.exe"
apiclaude --api-profile gpt-shell
```

Bridge nodes also default to the local proxy. Pass `--proxy-url direct` to
create or update one with the proxy disabled, or use `apiclaude --proxy`
afterward.

New bridge nodes use CPA. Existing bridge nodes without a `gateway` field retain
the previous LiteLLM path for compatibility; recreating them with
`apiclaude bridge` switches them to CPA. The prototype supports Claude Code's
streamed Messages and tool-use flow against an OpenAI-compatible Responses
endpoint. `--vscode` is rejected because an editor session needs a separately
managed persistent bridge lifecycle. Protocol translation can still behave
differently from a native Anthropic model, particularly for extended thinking
and newly introduced beta features.

### Claude Desktop native upstream and 3P bridge

Current Claude Desktop releases can use an officially supported third-party
inference gateway without an Anthropic account login. `apiclaude --desktop`
accepts both ordinary Claude API nodes created by `apiclaude --api-add` and
CPA-backed Codex bridge nodes:

```bash
apiclaude --desktop
apiclaude --desktop --api-profile anyrouter
apiclaude --desktop --api-profile codex-muyuan
```

For an ordinary node, the worker starts a loopback-only authenticated proxy and
forwards Anthropic Messages requests directly to that node's upstream. It does
not start CPA and does not convert requests to OpenAI Responses. `/v1/messages`,
streaming SSE, `/v1/messages/count_tokens`, `/v1/models`, and
`GET /v1/models/{id}` are supported. SSE events are flushed incrementally. The
configured node proxy setting is honored.

For a Codex bridge node, the existing CPA Messages-to-Responses translation is
unchanged. The normal command starts a hidden worker, assigns a free loopback
port, writes the node-local 3P configuration, and returns after the selected
gateway and Claude main process are both ready. The worker exits automatically
when that Claude window closes. Claude normally hides to the tray on the window
close action; the node worker detects that its main window stayed hidden, exits
the owned Desktop process, and then removes the matching gateway. No PowerShell
or bridge console remains visible. Use a fixed port only for diagnostics:

```bash
apiclaude --desktop --api-profile codex-muyuan --desktop-port 18765
```

Ordinary nodes discover their `claude-*` model IDs from `/v1/models` when the
Desktop worker starts. Discovery is bounded, preserves upstream order, removes
duplicates, and fails closed when the endpoint is unavailable or returns no
Claude model. Native nodes default to the 1M-context variant. Inspect the last
result, set an explicit model list, opt a node out of 1M, or restore automatic
discovery with:

```bash
apiclaude desktop-models anyrouter
apiclaude desktop-models anyrouter claude-sonnet-5 claude-haiku-4-5
apiclaude desktop-models anyrouter --1m
apiclaude desktop-models anyrouter --standard
apiclaude desktop-models anyrouter --auto
```

An explicit list skips discovery. Native models use identity routes, so Desktop
sends the selected `claude-*` ID unchanged; the GPT compatibility alias is not
added to ordinary nodes. A manual override is useful when an Anthropic-compatible
gateway implements Messages but does not expose `/v1/models`. Native nodes use
1M context by default: every configured model advertises `supports1m`, and the
first model also advertises `prefer1m`. For Messages and token-count requests,
the local proxy strips a trailing `[1m]` model suffix and merges Anthropic's 1M
beta header before forwarding upstream. `--standard` persists an explicit
opt-out and leaves request bodies and beta headers unchanged; `--1m` restores
the default. These flags do not change the model list and can also be combined
with an explicit list or `--auto`. Codex/CPA bridge nodes use standard context;
`desktop-models NODE --1m` is rejected because CPA does not implement this
Anthropic request variant.

The same node-level context mode applies to ordinary `apiclaude` / Claude Code
launches. On native nodes, the first explicitly configured Desktop model, then
the last discovered model list's first entry, becomes the CLI default and is
launched with its `[1m]` variant. An explicit Claude `--model` is upgraded to the
same variant. ApiClaude also supplies `--autocompact auto` unless the command
already specifies `--autocompact`, so long sessions compact automatically before
they reach the selected context limit. For example:

```bash
apiclaude --api-profile anyrouter --resume SESSION_ID
apiclaude --api-profile anyrouter --model claude-fable-5-1 --resume SESSION_ID
apiclaude --api-profile anyrouter --autocompact 180k --resume SESSION_ID
```

To let Claude Code reuse its saved model selection across launches, set
`"cli_force_default_model": false` on that node under `nodes` in
`~/.apiclaude_config.json`. When no `--model` is supplied, ApiClaude leaves model
selection to Claude Code and still provides `[1m]` family aliases from the
configured and discovered model lists. An explicit `--model` takes precedence,
and automatic compaction remains enabled. This setting defaults to `true`.

A CPA bridge node can also expose upstream Claude models by their native IDs.
Repeat `--desktop-model` when creating or updating the node:

```bash
apiclaude bridge prism --name codex-prism \
  --cpa-exe "C:/path/to/cli-proxy-api.exe" \
  --desktop-model claude-sonnet-5 \
  --desktop-model claude-sonnet-4-6 \
  --desktop-model claude-haiku-4-5
```

The Desktop gateway keeps the compatibility `claude-fable-5` route for the
node's primary GPT model and adds each requested native model as an identity
mapping. The first configured model for each Anthropic family is the family
default in Desktop. Model IDs are metadata only; the worker still loads the
upstream credential from the referenced Codex Profile at runtime.

Claude Code disables its Anthropic-hosted `WebSearch` implementation when the
inference provider is `gateway`. Both ordinary and CPA-backed Desktop nodes
therefore receive the same node-scoped `CLAUDE_CONFIG_DIR` used by that node's
CLI and VS Code launches, with a bundled, dependency-free `apiclaude-web` MCP
server. It exposes:

- `web_search`, which first asks the referenced Codex Profile's Responses
  endpoint to run its hosted `web_search` tool. A response counts as hosted
  search only when it contains a real `web_search_call`; unsupported routes,
  transport failures, and compatibility gateways that silently return an
  ordinary message fall back to Bing's public RSS search response.
- `get_weather`, backed by Open-Meteo geocoding and daily forecasts.

On CPA-backed nodes, the hosted search request bypasses CPA's Responses tool
conversion and uses a separate random token against the same in-memory loopback
authentication shim; the MCP process never receives the upstream API key. The
search token, shim address, and upstream model are inherited through the Claude
process environment and are not written to `.claude.json`. Ordinary native
nodes do not have a Responses search channel, so `web_search` uses the Bing RSS
fallback while `get_weather` remains available. The CPA bridge also repairs two known
Responses compatibility differences for native Claude models exposed by
OpenAI-compatible gateways: shortened MCP function names are restored only when
they map unambiguously to one declared tool, and CPA tool-result content-block
arrays are flattened to the Responses function-output string expected by the
upstream. Ambiguous tool names and ordinary function outputs are left unchanged.

Each bridge node keeps a compatibility root at
`~/.apiclaude-desktop/nodes/<node-slug>`. Claude Desktop 1.44121.4 and newer
resolve their effective user data and 3P configuration through the node-private
`.desktop-localappdata\Claude-3p` directory. Runtime state preserves the
compatibility root as `userDataDir` and reports the directory actually used by
current builds as `effectiveUserDataDir`; startup output displays the latter.
The gateway configuration is mirrored into both locations for older Desktop
releases. Desktop configuration, Recents, ordinary chat databases, logs, Cowork
files, local gateway token, MCP processes, and window state remain independent.
Desktop Code/Cowork transcripts are different: they use the node's canonical
Claude Code home, matching `apiclaude` and `apiclaude --vscode`. An isolated
node shares `~/.apiclaude/nodes/<slug>` across those three clients; a shared node
uses the normal `~/.claude` session home by design. Claude Code `/resume` can
therefore find Desktop Code/Cowork sessions for the same node without a manual
environment override. Ordinary Desktop chat records are not Claude Code JSONL
sessions and do not appear in that picker.

On the first Desktop start after upgrading, valid UUID-named JSONL transcripts
from the former node-private `claude-code-config/projects` directory are copied
into the canonical home. Existing identical files are left alone, conflicting
same-ID files abort startup without overwriting either copy, while a canonical
copy that only appended to the complete legacy transcript is recognized as the
same migrated session. The legacy source remains available for rollback.
Different nodes can run concurrently on
different ports. Starting the same node again reports the existing PID and does
not create a second process against the same Desktop data directory.

Manage the workers without finding processes manually:

```bash
apiclaude --desktop-status
apiclaude --desktop-status --api-profile codex-muyuan
apiclaude --desktop-stop --api-profile codex-muyuan
apiclaude --desktop-foreground --api-profile codex-muyuan
```

Foreground mode keeps lifecycle output in the terminal for troubleshooting.
Normal worker logs are stored under the node's `.apiclaude-runtime` directory
and contain no API keys. The current signed Windows MSIX package is required:
the launcher invokes its main executable directly with `--user-data-dir` before
Electron's single-instance lock and supplies the node-private `LOCALAPPDATA` for
3P configuration discovery. Shell activation and legacy EXE fallback are
deliberately not used because they cannot guarantee profile isolation.

The upstream API key remains in the ordinary Claude node or referenced Codex
Profile `SecureStore` and is loaded only inside the hidden worker. It is never
written to a command line, Desktop configuration, CPA YAML, runtime state, or
log. The random per-node loopback token is not an upstream credential:
Desktop's static Gateway mode requires it in the node-local config, and CPA
needs it in its short-lived YAML. The whole node directory is protected by a
non-inherited ACL for the current user, Windows SYSTEM, and administrators. The
local token can be regenerated or inspected for diagnostics through
`apiclaude desktop-token NODE`.

Claude Desktop validates Gateway model routes against its Anthropic model
catalog. The Desktop bridge therefore advertises the recognized local route
`claude-fable-5`; CPA force-maps that route to the bridge node's actual GPT
model. The display name should identify the actual GPT model. This compatibility
alias is Desktop-only and does not change the regular Claude Code bridge route.

Desktop third-party inference is an official Claude Desktop feature. Native
ordinary nodes are limited to Anthropic Messages-compatible `claude-*` models;
GLM, GPT, and other non-Claude routes continue to require a Codex bridge node.
Using a non-Anthropic model through CPA remains an experimental protocol
translation and is not supported by Anthropic. Each worker serves one node-local
gateway and its configured model routes for the lifetime of the corresponding
Desktop process.

On first launch, a bridge node adds a node-local
`skillOverrides.claude-api = "user-invocable-only"` default when that skill has
no explicit override. This prevents a non-Anthropic model from automatically
loading Claude Code's large bundled Anthropic API reference for simple
model-identification questions. `/claude-api` remains available for explicit
use, existing settings and explicit skill choices are preserved, and regular
Claude nodes are not changed.

Some Codex Profile gateways reject generic HTTP clients even when the API key
is valid. Bridge requests therefore use the transparent Codex-compatible
identity `codex_cli_rs/apiclaude-bridge` with originator
`apiclaude_codex_bridge`; they do not claim to be an official Codex build.
Only use this mode with gateways whose terms allow compatible third-party
clients.

Choose a node and start Claude Code:

```bash
apiclaude
```

Start a specific node without prompting, with any Claude Code arguments:

```bash
apiclaude --api-profile mysub2api
apiclaude --api-profile mysub2api resume
```

Pass Claude Code arguments after `apiclaude`:

```bash
apiclaude --yolo
apiclaude --permission-mode bypassPermissions
apiclaude resume
apiclaude -c
```

`apiclaude --yolo` is a shortcut for
`apiclaude --permission-mode bypassPermissions`, matching the convenience of
`apicodex --yolo`. It can also be combined with `--api-profile` and other
Claude Code arguments.

Other management commands:

```bash
apiclaude --api-list
apiclaude --api-list --json
apiclaude --api-remove
apiclaude --up
apiclaude --api-help
apiclaude current
```

`--api-list --json` emits schema-versioned, non-sensitive node metadata,
including the node mode, config directory, VS Code user-data directory, and
`lastUsedAt`. It never includes tokens or credential-store identifiers.

Open VS Code with a node-scoped user-data directory:

```bash
apiclaude --vscode
apiclaude --vscode --api-profile mysub2api
```

The selected node's API endpoint and token are passed only through the VS Code
child-process environment. Isolated nodes also receive their
`CLAUDE_CONFIG_DIR`; shared nodes continue to use `~/.claude`. If a VS Code
process for that node is already running when its token changes, close all
windows for the node and reopen it so the new child environment takes effect.

`apiclaude --up` delegates to the official `claude update` command without
loading a node token or config directory.

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

- On Windows, encryption is bound to the current user through DPAPI. On macOS,
  credentials are stored as generic passwords in the user's login Keychain.
  ApiAgent v2 Keychain items allow non-interactive access within the logged-in
  user session, so replacing or updating the Python interpreter does not trigger
  repeated password dialogs. No application-specific master password is required.
- Existing macOS v1 Keychain items are copied to v2 on first use, read back for
  exact verification, and then removed. That one-time read may require a final
  Keychain confirmation; subsequent loads use v2 without prompting. If both
  versions exist with different values, ApiAgent rejects the conflict instead of
  silently sending an ambiguous credential. Node listings never read values.
- Existing plaintext Claude `token` fields and Codex `auth.json` API keys are
  migrated on the first `apiclaude` or `apicodex` load.
- Migration writes and reads back the encrypted value before removing plaintext
  from configuration files.
- Normal account login state under `~/.codex` and `~/.claude` is not changed.
- `apicodex` disables ChatGPT-hosted apps/plugins for API profiles so the CLI
  does not attempt unavailable `codex_apps` host authentication. This does not
  affect ordinary `codex` account sessions.
- Python CLI secure credential storage is supported on Windows and macOS. A
  Linux secure backend is not implemented yet.
