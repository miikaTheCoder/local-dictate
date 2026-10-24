# Shared native ASR server

The current machine already has an enabled user service and both the original v3 and validated Ultra models. Ultra is now selected through the reversible model-manager drop-in; the original base unit and v3 files remain intact. See [model control](MODELS.md) and [verified activation/rollback](ULTRA-ACTIVATION.md). Installing the desktop client does not need any command in the download or service-install sections. Do not download a duplicate model or run another server on port 6006. The clean-machine examples below bootstrap v3, not an automatic Ultra upgrade.

## Network isolation

The v1.13.8 native offline WebSocket server listens on **`0.0.0.0:6006`**. Its installed `--help` has no host/listen-address option. Configuring Local Dictate to connect to `ws://127.0.0.1:6006` with proxies disabled restricts this client's destination, not the server's inbound exposure. Recognition still runs locally on CPU, without a cloud service.

The existing user unit contains `IPAddressDeny=any` and `IPAddressAllow=localhost`, but a local audit on 2026-10-24 confirmed they are **ineffective on this machine**. The user journal reports `unit configures an IP firewall, but not running as root.` The audited environment has systemd 262.1 with BPF and kernel cgroup BPF support, and `kernel.unprivileged_bpf_disabled=2`. See [systemd issue 38588](https://github.com/systemd/systemd/issues/38588) and [systemd.resource-control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html#IPAddressAllow=). The optional user-service template deliberately omits these ineffective directives. The existing unit is left untouched.

UFW was observed installed and active, with configured default input DROP and a loopback accept rule. Its readable user rules also allow the `tailscale0` interface broadly. Static configuration alone does not prove LAN or Tailscale isolation: active kernel rules, earlier accept chains, and Tailscale chain ordering were not verified. Do not describe the current server as securely loopback-isolated. Client setup does not refuse the existing server on this basis.

The minimal user-service deployment remains runnable, with that exposure caveat. To enforce isolation separately, an administrator can choose either:

- **Host firewall:** deny inbound TCP 6006 on non-loopback interfaces, including VPN interfaces, while preserving loopback. Review actual IPv4/IPv6 rule order, UFW, and any Tailscale-managed chains. A UFW rule alone may be bypassed by an earlier accept chain. Check localhost still works and verify connection failure from an actual separate LAN/Tailscale host before claiming isolation.
- **Root-managed system service:** use a system unit with `User=nextg` and `Group=nextg`, absolute paths to the existing binary/model, and `IPAddressDeny=any` plus `IPAddressAllow=localhost`. The system manager installs the cgroup IP filter while the server process runs unprivileged. This requires supported and working kernel/BPF enforcement and verification. In system units `%h` refers to the manager's home, so do not copy the user template's `%h` paths unchanged. Stop the user instance before starting a system instance to avoid competing servers. This is an optional administrator-managed migration, not part of client installation.

No privileged firewall operation, system-unit install, namespace launcher, or security migration is automated here. The preferred user service remains unchanged. A loopback success test alone is not an off-host isolation test.

## New-machine downloads

For a clean **Linux x86_64** Omarchy / Arch machine only. Install the small dependencies listed in [README.md](../README.md#clean-omarchy--arch-installation) first. These instructions use the native **CPU static v1.13.8** release. They do not install a Python inference engine.

Exact upstream assets:

- [sherpa-onnx-v1.13.8-linux-x64-static.tar.bz2](https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.8/sherpa-onnx-v1.13.8-linux-x64-static.tar.bz2), from the [v1.13.8 release](https://github.com/k2-fsa/sherpa-onnx/releases/tag/v1.13.8), source tag `v1.13.8`, commit [`11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf`](https://github.com/k2-fsa/sherpa-onnx/commit/11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf).
- [sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2), from the [asr-models release](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models). Required extracted files are `encoder.int8.onnx`, `decoder.int8.onnx`, `joiner.int8.onnx`, and `tokens.txt`.

The `asr-models` tag is a changing asset collection, so the filename alone is not an immutable content pin. The following SHA-256 values were read from existing local archives and independently matched the upstream GitHub [binary asset metadata](https://github.com/k2-fsa/sherpa-onnx/releases/expanded_assets/v1.13.8) and [model asset metadata](https://github.com/k2-fsa/sherpa-onnx/releases/expanded_assets/asr-models) during this documentation pass:

```text
d265986f7b990026e45bca573415575140106a2c663014924037d8f2c2e297cc  sherpa-onnx-v1.13.8-linux-x64-static.tar.bz2
5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf  sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2
```

These checksums pin the observed release bytes. They are not signatures or an independent audit of the upstream publisher. No new binary/model download or clean-machine installation was performed to prepare this guide. Local archives are under `/home/nextg/Work/pi-dictate-local-runtime/downloads`; the local model archive is named `parakeet.tar.bz2`.

Run the following in Bash on the new machine. It downloads to private staging, requires HTTPS, fails on HTTP errors or checksum changes, rejects unexpected archive roots/traversal, uses Python's data extraction filter, and refuses existing final directories. It requires Python 3.12+ for safe extraction, as provided by this Arch setup. Allow room for archives and extraction, roughly a few GiB. Do not run it as root.

```bash
(
  set -euo pipefail
  umask 077
  [[ $(uname -m) = x86_64 ]] || { echo 'This release is for x86_64.' >&2; exit 1; }
  asr_root="$HOME/.local/share/local-dictate"
  server_name=sherpa-onnx-v1.13.8-linux-x64-static
  model_name=sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8
  for target in "$asr_root/server/$server_name" "$asr_root/models/$model_name"; do
    [[ ! -e $target && ! -L $target ]] || { echo "Already exists: $target" >&2; exit 1; }
  done
  mkdir -p -- "$asr_root/server" "$asr_root/models"
  asr_stage=$(mktemp -d "$asr_root/.setup.XXXXXXXX")
  trap 'rm -rf -- "$asr_stage"' EXIT
  curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 \
    --output "$asr_stage/$server_name.tar.bz2.part" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.8/$server_name.tar.bz2"
  mv -- "$asr_stage/$server_name.tar.bz2.part" "$asr_stage/$server_name.tar.bz2"
  curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 \
    --output "$asr_stage/$model_name.tar.bz2.part" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$model_name.tar.bz2"
  mv -- "$asr_stage/$model_name.tar.bz2.part" "$asr_stage/$model_name.tar.bz2"
  (
    cd -- "$asr_stage"
    sha256sum --check <<'SUMS'
d265986f7b990026e45bca573415575140106a2c663014924037d8f2c2e297cc  sherpa-onnx-v1.13.8-linux-x64-static.tar.bz2
5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf  sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2
SUMS
  )
  python3 - "$asr_stage" "$server_name" "$model_name" <<'PY'
import pathlib
import sys
import tarfile

if sys.version_info < (3, 12):
    raise SystemExit('Safe extraction requires Python 3.12+.')
stage = pathlib.Path(sys.argv[1])
for name in sys.argv[2:]:
    destination = stage / 'extracted' / name
    destination.mkdir(parents=True)
    with tarfile.open(stage / (name + '.tar.bz2'), 'r:bz2') as archive:
        members = archive.getmembers()
        for member in members:
            path = pathlib.PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != name:
                raise SystemExit('Unexpected archive path: ' + member.name)
        archive.extractall(destination, members=members, filter='data')
PY
  test -x "$asr_stage/extracted/$server_name/$server_name/bin/sherpa-onnx-offline-websocket-server"
  for file in encoder.int8.onnx decoder.int8.onnx joiner.int8.onnx tokens.txt; do
    test -s "$asr_stage/extracted/$model_name/$model_name/$file"
  done
  mv -nT -- "$asr_stage/extracted/$server_name/$server_name" "$asr_root/server/$server_name"
  mv -nT -- "$asr_stage/extracted/$model_name/$model_name" "$asr_root/models/$model_name"
  "$asr_root/server/$server_name/bin/sherpa-onnx-offline-websocket-server" --help
)
```

If the network/DNS fails or a checksum changes, stop and investigate the upstream release instead of weakening verification. The staging directory is removed on exit, including partial downloads. Files already installed by an earlier successful run are preserved. Keep the original archive/license provenance if redistributing these components. [Attribution and licenses](ATTRIBUTION.md) identify the MIT reference client, Apache-2.0 server, and NVIDIA CC BY 4.0 model.

## Optional new-machine user service

Read the [network isolation caveat](#network-isolation) first. This path is runnable as a user service but does not itself restrict the wildcard listener. Never install it over the existing Pi unit.

From the project checkout, after downloading/extracting on the new machine:

```bash
(
  set -euo pipefail
  unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  unit="$unit_dir/sherpa-onnx-asr.service"
  [[ ! -e $unit && ! -L $unit ]] || { echo "Existing unit preserved: $unit" >&2; exit 1; }
  mkdir -p -- "$unit_dir"
  (set -o noclobber; cat systemd/sherpa-onnx-asr.service > "$unit")
  systemd-analyze --user verify "$unit"
)
```

Inspect `systemctl --user cat sherpa-onnx-asr.service` for any other unit/drop-ins before enabling. Once you choose the exposure policy, explicitly load and enable the new unit:

```bash
systemctl --user daemon-reload
systemctl --user enable --now sherpa-onnx-asr.service
systemctl --user status sherpa-onnx-asr.service
journalctl --user -u sherpa-onnx-asr.service -n 50 --no-pager
```

The [template](../systemd/sherpa-onnx-asr.service) uses `--provider=cpu --num-work-threads=1 --num-threads=4 --num-io-threads=1 --max-batch-size=1`. All these flags, the four model paths, `--port`, and `--log-file` were checked against the installed v1.13.8 binary's `--help`. Model type is autodetected from the ONNX metadata; no unverified model-type value is required. Tune `--num-threads` between **1 and 4** for your CPU, prioritizing desktop responsiveness. Single-user dictation does not need a large batch or multiple inference workers. This is a recommendation, not a measured performance result for each setting.

The template uses `--log-file=/dev/null` to avoid a persistent ASR log. Service startup/failure information may still appear in the journal. The existing shared Pi unit instead points to its own `server.log`, and is not changed here. Do not share existing logs without checking for private content. Never enable debug transcript logging as a default.

## Existing-machine optional tuning

**With the model manager installed, edit `[server]` in `~/.config/local-dictate/asr.toml`, then use `local-dictate-model check` and `apply`.** Do not install the legacy v3-only `50-local-dictate-cpu.conf` alongside `60-local-dictate-model.conf`: both replace `ExecStart`, and the model manager's later file wins. The following legacy recipe applies only when not using model management.

The current enabled symlink targets `/home/nextg/Work/pi-dictate-local-runtime/sherpa-onnx-asr.service`, with `--num-work-threads=4 --num-threads=8`. Leave it as-is for client installation. If you deliberately choose to reduce concurrency later, [current-machine-low-concurrency.conf](../examples/current-machine-low-concurrency.conf) is an optional drop-in with the exact existing binary/model paths and `--log-file=/dev/null`. It neither copies the model nor resolves network isolation.

First inspect the merged unit and preserve a snapshot:

```bash
systemctl --user cat sherpa-onnx-asr.service
mkdir -p -- "$HOME/.local/state/local-dictate"
systemctl --user cat sherpa-onnx-asr.service > "$HOME/.local/state/local-dictate/asr-unit.$(date +%Y%m%d-%H%M%S).txt"
```

From the checkout, manually install only this new drop-in without overwriting an existing one:

```bash
(
  set -euo pipefail
  dropin_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/sherpa-onnx-asr.service.d"
  mkdir -p -- "$dropin_dir"
  (set -o noclobber; cat examples/current-machine-low-concurrency.conf > "$dropin_dir/50-local-dictate-cpu.conf")
)
systemctl --user daemon-reload
systemctl --user cat sherpa-onnx-asr.service
```

Review the merged unit before an intentional `systemctl --user restart sherpa-onnx-asr.service`. Restarting interrupts **both Pi and Local Dictate**. Nothing in the client installer performs these commands. To undo just this optional tuning, remove only `50-local-dictate-cpu.conf`, run `systemctl --user daemon-reload`, review the merged unit, then deliberately restart. Do not use `systemctl revert`, which can remove unrelated overrides.

## Manage the shared service

These commands are manual server lifecycle operations, separate from client setup:

```bash
systemctl --user status sherpa-onnx-asr.service
systemctl --user is-enabled sherpa-onnx-asr.service
systemctl --user cat sherpa-onnx-asr.service
journalctl --user -u sherpa-onnx-asr.service -n 50 --no-pager
systemctl --user start sherpa-onnx-asr.service
systemctl --user stop sherpa-onnx-asr.service
systemctl --user restart sherpa-onnx-asr.service
systemctl --user enable sherpa-onnx-asr.service
systemctl --user disable sherpa-onnx-asr.service
```

`start` loads the model now; `stop` frees the resident model memory and makes both clients unavailable. `restart` interrupts active requests and reloads the model. `enable` starts it on future user-manager startup; `disable` removes that startup link but does not stop the running service. Use `disable --now` only if you deliberately want both effects. No login lingering is required by this desktop client.

For an edited unit or drop-in, `daemon-reload` updates systemd's definition and an intentional restart applies a changed command. If startup fails, inspect status/journal, validate binary and all four model paths, verify template flags with the binary's `--help`, and check for another listener using `ss -ltn '( sport = :6006 )'`. Do not start competing ASR processes or redownload a working model as a first repair. Check `systemctl --user --failed` after addressing an error.

Uninstalling the desktop client must preserve this machine's ASR unit, Pi source/runtime/model files, and any unrelated overrides. Only on a separate machine where you installed a dedicated ASR service and no longer need any client should you deliberately disable/stop and remove that service. Review each model directory before deleting it; never remove the shared current-machine model as part of Local Dictate uninstall.
