#!/usr/bin/env bash
# verify — run OpenJML ESC on Solution.java (takes no arguments).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/harness/_env.sh"
exec "$PYTHON" "$SCRIPT_DIR/harness/cli.py" verify "$@"
