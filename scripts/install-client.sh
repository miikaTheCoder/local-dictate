#!/usr/bin/env bash
# Install only the desktop client link and a new user configuration.
set -euo pipefail
umask 077

die() { printf 'local-dictate setup: %s\n' "$*" >&2; exit 1; }

[[ $# -eq 0 ]] || die "Usage: $0 (no arguments)"
[[ -n ${HOME:-} && $HOME = /* ]] || die 'HOME must be an absolute directory.'

script=$(readlink -f -- "${BASH_SOURCE[0]}")
source_dir=$(cd -- "$(dirname -- "$script")/.." && pwd -P)
launcher="$source_dir/bin/local-dictate"
example="$source_dir/config.example.toml"
destination="$HOME/.local/bin/local-dictate"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/local-dictate"
[[ $config_dir = /* ]] || die 'XDG_CONFIG_HOME must be absolute when set.'
[[ -x $launcher ]] || die "Missing executable: $launcher"
[[ -f $example && -r $example ]] || die "Missing readable config example: $example"

# Match bin/local-dictate's interpreter selection exactly.
if [[ -n ${LOCAL_DICTATE_PYTHON:-} ]]; then
    python=$LOCAL_DICTATE_PYTHON
elif [[ -x $source_dir/.venv/bin/python ]]; then
    python="$source_dir/.venv/bin/python"
else
    python=/usr/bin/python3
fi
command -v "$python" >/dev/null || die "Missing Python interpreter: $python"
"$python" - <<'PY' || die 'Python 3.11+ and websockets 15+ are required. See README.md.'
import inspect
import sys
import websockets
from websockets.asyncio.client import connect

if sys.version_info < (3, 11) or 'proxy' not in inspect.signature(connect).parameters:
    raise SystemExit('Need Python 3.11+ and websockets with proxy=None support.')
print(f'Python {sys.version.split()[0]}, websockets {websockets.__version__}')
PY

for tool in parec wtype wl-copy hyprctl; do
    command -v "$tool" >/dev/null || die "Missing $tool. See README.md for manual dependencies."
done
if ! command -v notify-send >/dev/null; then
    printf 'Warning: notify-send is missing; desktop notifications will be unavailable.\n' >&2
fi

# Check conflicts before creating either output. ln without -f also protects races.
if [[ -e $destination || -L $destination ]]; then
    [[ -L $destination && $(readlink -f -- "$destination") = "$launcher" ]] ||
        die "Refusing to replace existing command: $destination"
    [[ $(stat -c %u -- "$destination") = "$(id -u)" ]] ||
        die "Command symlink is not owned by this user: $destination"
fi
[[ ! -d $config_dir/config.toml ]] || die "Config path is a directory: $config_dir/config.toml"

mkdir -p -- "$HOME/.local/bin" "$config_dir"
[[ -O $HOME/.local/bin && -O $config_dir ]] || die 'Install directories must be owned by this user.'
if [[ ! -L $destination ]]; then
    ln -sT -- "$launcher" "$destination"
fi
if [[ -e $config_dir/config.toml || -L $config_dir/config.toml ]]; then
    printf 'Preserved existing config: %s\n' "$config_dir/config.toml"
else
    # noclobber refuses an existing file or symlink, including a concurrent creation.
    (set -o noclobber; cat -- "$example" > "$config_dir/config.toml")
    printf 'Created config: %s\n' "$config_dir/config.toml"
fi
printf 'Client link: %s -> %s\n' "$destination" "$launcher"
printf 'No packages, ASR services, models, or Hyprland bindings changed.\n'
