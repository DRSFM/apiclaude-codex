# apiagent / apicodex / apiclaude

Cross-platform API profile launchers for Codex CLI and Claude Code.

- `apicodex` manages Codex API profiles under `~/.codex-api`.
- `apiclaude` manages Claude Code API nodes in `~/.apiclaude_config.json`.
- `apiagent` is a shared entrypoint for both.

The config files contain API keys/tokens and should not be committed.

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

Run a specific profile:

```bash
apicodex --api-profile bohe resume
```

Other management commands:

```bash
apicodex --api-remove
apicodex --api-help
```

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
