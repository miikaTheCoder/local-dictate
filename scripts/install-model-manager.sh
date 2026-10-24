#!/usr/bin/env bash
# Install only the separate model manager link and first ASR configuration.
set -euo pipefail
umask 077
[[ $# -eq 0 ]] || { printf 'Usage: %s (no arguments)\n' "$0" >&2; exit 1; }
script=$(readlink -f -- "${BASH_SOURCE[0]}")
source_dir=$(cd -- "$(dirname -- "$script")/.." && pwd -P)
if [[ -n ${LOCAL_DICTATE_PYTHON:-} ]]; then
    python=$LOCAL_DICTATE_PYTHON
elif [[ -x $source_dir/.venv/bin/python ]]; then
    python="$source_dir/.venv/bin/python"
else
    python=/usr/bin/python3
fi
exec "$python" -B - "$source_dir" <<'PY'
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11+ is required.')
sys.path.insert(0, sys.argv[1])
from model_manager import ModelError, install

try:
    install(Path(sys.argv[1]))
except (ModelError, OSError):
    error = sys.exception()
    message = str(error) if isinstance(error, ModelError) else 'Filesystem operation failed; existing files were preserved.'
    raise SystemExit('local-dictate-model setup: ' + message)
PY
