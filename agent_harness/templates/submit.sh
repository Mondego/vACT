#!/usr/bin/env bash
# submit — record the final submission (writes submission.json). Usage: ./submit.sh "summary"
# The final code is that of the last accepted verify/harness call. Every argument is taken as the
# summary text, even one starting with '-'.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/harness/_env.sh"
exec "$PYTHON" "$SCRIPT_DIR/harness/cli.py" submit -- "$@"
