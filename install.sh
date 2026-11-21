#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"

CREATE_DESKTOP_SHORTCUT=false
if [[ $# -gt 0 ]]; then
    case "$1" in
        --desktop-shortcut)
            CREATE_DESKTOP_SHORTCUT=true
            shift
            ;;
        --help|-h)
            echo "Usage: ./install.sh [--desktop-shortcut]"
            echo "  --desktop-shortcut  Also install the macOS Desktop profile picker."
            exit 0
            ;;
        *)
            echo "Error: unknown option: $1" >&2
            exit 1
            ;;
    esac
fi
if [[ $# -ne 0 ]]; then
    echo "Error: usage: ./install.sh [--desktop-shortcut]" >&2
    exit 1
fi

SHORTCUT="${HOME}/Desktop/ChatGPT 多实例.command"
if [[ "$CREATE_DESKTOP_SHORTCUT" == true ]]; then
    if [[ "$(uname -s)" != Darwin ]]; then
        echo "Error: --desktop-shortcut requires macOS." >&2
        exit 1
    fi
    if [[ -L "$SHORTCUT" ]] || { [[ -e "$SHORTCUT" ]] && ! cmp -s "$SCRIPT_DIR/apicodex-desktop.command" "$SHORTCUT"; }; then
        echo "Error: an existing shortcut differs; move it aside before installing: $SHORTCUT" >&2
        exit 1
    fi
fi

mkdir -p "$BIN_DIR"
chmod +x "$SCRIPT_DIR/apiagent" "$SCRIPT_DIR/apicodex" "$SCRIPT_DIR/apiclaude"

ln -sf "$SCRIPT_DIR/apiagent.py" "$BIN_DIR/apiagent.py"
ln -sf "$SCRIPT_DIR/apiagent" "$BIN_DIR/apiagent"
ln -sf "$SCRIPT_DIR/apicodex" "$BIN_DIR/apicodex"
ln -sf "$SCRIPT_DIR/apiclaude" "$BIN_DIR/apiclaude"

echo "Installed:"
echo "  $BIN_DIR/apiagent"
echo "  $BIN_DIR/apicodex"
echo "  $BIN_DIR/apiclaude"

if [[ "$CREATE_DESKTOP_SHORTCUT" == true ]]; then
    mkdir -p "${HOME}/Desktop"
    cp "$SCRIPT_DIR/apicodex-desktop.command" "$SHORTCUT"
    chmod 700 "$SHORTCUT"
    echo "  $SHORTCUT"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo
    echo "Note: $BIN_DIR is not in PATH for this shell."
    echo "Add this line to your shell config, then restart the terminal:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac
