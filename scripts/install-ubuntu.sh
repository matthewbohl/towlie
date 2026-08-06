#!/usr/bin/env bash
set -euo pipefail

echo "This installer name is deprecated; using the Raspberry Pi OS installer." >&2
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/install-raspberry-pi-os.sh" "$@"
