#!/usr/bin/env bash
# run_spec_harness — score the spec in Solution.java on the four spec-harness metrics
# (first 5 feedback pairs; optional: --max-pairs N).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/harness/_env.sh"
exec "$PYTHON" "$SCRIPT_DIR/harness/cli.py" harness "$@"
