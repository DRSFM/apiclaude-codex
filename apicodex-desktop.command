#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
APICODEX_LAUNCHER="${HOME}/.local/bin/apicodex"

if [[ ! -x "$APICODEX_LAUNCHER" ]]; then
    echo "Error: ApiCodex is not installed. Run ./install.sh in the repository first." >&2
    exit 1
fi

exec "$APICODEX_LAUNCHER" --desktop "$@"
