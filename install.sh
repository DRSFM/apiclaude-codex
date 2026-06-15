#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"

mkdir -p "$BIN_DIR"
chmod +x "$SCRIPT_DIR/apiagent" "$SCRIPT_DIR/apicodex" "$SCRIPT_DIR/apiclaude"

ln -sf "$SCRIPT_DIR/apiagent" "$BIN_DIR/apiagent"
ln -sf "$SCRIPT_DIR/apicodex" "$BIN_DIR/apicodex"
ln -sf "$SCRIPT_DIR/apiclaude" "$BIN_DIR/apiclaude"

echo "Installed:"
echo "  $BIN_DIR/apiagent"
echo "  $BIN_DIR/apicodex"
echo "  $BIN_DIR/apiclaude"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo
    echo "Note: $BIN_DIR is not in PATH for this shell."
    echo "Add this line to your shell config, then restart the terminal:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac
