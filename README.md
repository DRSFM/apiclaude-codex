# apiclaude

`apiclaude` is a small Claude Code API node switcher. It stores API nodes in
`~/.apiclaude_config.json`, lets you choose a node, then launches `claude` with
the selected `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`.

The config file contains tokens and should not be committed.

## Requirements

- Python 3
- Claude Code CLI available as `claude`

Check:

```bash
python3 --version
claude --version
```

On Windows:

```powershell
python --version
claude --version
```

## Install On macOS Or Linux

Clone the repo, then link the launcher into your PATH:

```bash
mkdir -p ~/.local/bin
chmod +x apiclaude
ln -sf "$(pwd)/apiclaude" ~/.local/bin/apiclaude
```

If `~/.local/bin` is not in PATH:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

## Install On Windows

Put this repository somewhere stable, for example `C:\tools\apiclaude`, then add
that folder to PATH. Run:

```powershell
apiclaude.bat
```

If you want the command to be exactly `apiclaude`, keep `apiclaude.bat` in a
folder that is already in PATH.

## Usage

Add a node:

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
