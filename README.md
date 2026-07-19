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
