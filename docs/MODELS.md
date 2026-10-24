# Config-driven model selection

`local-dictate-model` manages the existing CPU `sherpa-onnx-asr.service` through one private user drop-in. It shares the server with Pi. Alt+C, Pi's binding, and the client endpoint `ws://127.0.0.1:6006` stay the same. Model switching performs a controlled restart, which interrupts both clients and reloads weights. It is not an in-memory hot swap.

The default template selects **Parakeet TDT 0.6B v3 int8**, using the existing absolute model directory. The pinned Ultra profile has now passed native CPU recognition on this machine; see [the validation report](ULTRA-NATIVE-VALIDATION.md). The template still requires explicit external-validation attestation on a new installation. Use `list` to check the actual running model rather than infer it from the template. The manager never downloads packages, weights, or another server, and never removes or modifies either model directory.

## Install the separate manager

From the permanent checkout:

```bash
bash scripts/install-model-manager.sh
"$HOME/.local/bin/local-dictate-model" list
"$HOME/.local/bin/local-dictate-model" check
```

The installer creates only the `~/.local/bin/local-dictate-model` symlink and the first `~/.config/local-dictate/asr.toml`, plus missing parent directories. It preserves existing configuration, including dangling symlinks and permissions. A conflicting command is refused. It does not install or restart a service, edit hotkeys, contact ASR, or invoke desktop tools. Keep the checkout at its installed location.

Python selection matches the desktop launcher: `LOCAL_DICTATE_PYTHON`, then checkout `.venv/bin/python`, then `/usr/bin/python3`. Python 3.11+ is required. Only activation health checks additionally need the client's existing `websockets` dependency. No ASR Python library is used. Launchers disable bytecode writes.

`XDG_CONFIG_HOME` replaces `~/.config` when set. The separate `config.toml` still controls the desktop client. Use `local-dictate-model --config /absolute/path/asr.toml COMMAND` for another manager configuration. The managed service and recovery locations still use the XDG directories, so changing `--config` does not create an independent server.

## Edit the desired model

[asr.example.toml](../asr.example.toml) is the complete starting configuration. A named profile supplies an absolute `directory`, four distinct filenames (`encoder`, `decoder`, `joiner`, `tokens`), and `validated`. Filenames are configurable because a verified Ultra export may use different names. Optional per-profile `feat_dim` is an integer from 1 to 256, such as 128 when confirmed for the export. Optional `model_type` accepts `transducer` or the current baseline's `nemo_transducer` hint. Omit both to use native defaults and metadata detection. Presence of the `--model-type` flag in help does not verify a hint's suitability for a particular export; external native validation still determines that. Parent traversal, control characters, arbitrary extra flags, and unknown keys are rejected.

An agent should follow this workflow:

1. Read `list`, the example, and the current desired configuration. Preserve v3 and unrelated settings.
2. Obtain independent evidence for Ultra provenance, the compatible 0.6B int8 export, native CPU compatibility, resource use, and recognition. Stage approved files separately using a process outside this manager. No download is part of `check` or `apply`.
3. Confirm the Ultra staging directory and all four expected filenames against the verified local export. Set any required native overrides, then set `validated = true` only after that external evaluation. This field records the operator's decision; the manager cannot independently establish provenance, quantization, parameter count, ONNX metadata compatibility, or recognition quality.
4. Run `local-dictate-model check --model parakeet-ultra-int8`. This checks nonempty regular readable files, the executable, and advertised native flags. It does not load the model or perform recognition.
5. Once activation is authorized, set `active_model = "parakeet-ultra-int8"`, stop dictation in both clients, then run `local-dictate-model check` and `local-dictate-model apply`.
6. Review the result and `local-dictate-model list`. Perform any explicitly authorized recognition and resource checks separately. Use `rollback` if needed.

Editing TOML changes only the desired selection. It has no effect on a running server until `apply` succeeds. `list` shows desired and observed process profiles separately where inspection is available. "Available locally" means files and external-validation attestation are present; `check` still validates the binary and flags. Neither label promises recognition quality.

## Commands and safeguards

```bash
local-dictate-model list
local-dictate-model check
local-dictate-model check --model parakeet-ultra-int8
# The following two commands deliberately change the shared service:
local-dictate-model apply
local-dictate-model rollback
```

`list` and `check` do not write runtime/recovery files, reload units, restart services, or capture audio. `check` prints the prospective drop-in and runs the selected trusted binary only with `--help`, under a five-second timeout. No server instance is launched by that inspection.

The server configuration permits only CPU and port 6006. Moving the port requires a coordinated implementation/configuration change for both clients, so this manager rejects other ports. Initial concurrency matches the existing service: four work threads, eight inference threads, one I/O thread, and the native default batch size of five. Model type is autodetected unless the profile explicitly supplies a hint. Generated commands explicitly use CPU, `/dev/null` for the native log file, and `--print-args=false`.

Bounds are 1 to 16 work threads, 1 to 32 inference threads, 1 to 8 I/O threads, and 1 to 32 batch items. `health_timeout` is an integer from 5 to 180 seconds, default 60. Every systemctl command has a ten-second timeout. Health polling shares its configured total deadline across command inspection, listener ownership checks, and WebSocket attempts. Each phase needs positive remaining time, and readiness is rejected if the deadline expires before success is reported. A failed activation can take another readiness deadline to verify recovery. The CLI does not wait indefinitely for model loading.

Before switching, the manager checks the existing Local Dictate owner lock and, for an active owner, its read-only control status. An active or unresponsive session blocks switching. It does not invoke the desktop `status` command, which can clean up client runtime files. `XDG_RUNTIME_DIR` must identify the private runtime directory. The manager creates no client runtime files. **Pi activity cannot be detected. Stop Pi dictation first and keep both clients idle throughout activation.** A session started after the idle check can still race a restart; the manager's lock serializes model operations, not all client activity.

Apply requires a regular executable and four nonempty regular model files before any manager write or service action. Symlink model files are rejected. Management paths reject symlink ancestors, unsafe ownership, writable foreign directories, and unsafe lock/recovery files. The config must be user-owned, regular, single-linked, and not writable by group or others. Existing config symlinks are preserved by installation but deliberately rejected by the manager. Private state and drop-in files must have no group/other permissions. Existing files are never chmodded to force acceptance. Model paths containing spaces, quotes, dollars, backslashes, and percent signs are safely quoted for systemd. Systemd rejects quotes and backslashes in the executable path, so validation rejects those binary names. Newlines and other control characters are rejected everywhere. No shell is used to launch the server, and no environment-secret fields are accepted.

## Transaction and recovery

The only service file owned by this tool is:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/sherpa-onnx-asr.service.d/60-local-dictate-model.conf
```

It contains an ownership marker, resets `ExecStart`, and supplies the explicit command. A foreign file at this path is refused. The base unit, original v3 files, Pi extension, unrelated drop-ins, enablement, and firewall settings are preserved. Another drop-in can override this command; command verification will reject an unexpected running process rather than declare success. Do not use `systemctl revert` for recovery because that can remove unrelated overrides.

The manager takes an exclusive nonblocking lock under:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/local-dictate/model-manager/
```

Before activation it atomically saves `pending.json`, containing the prior managed file's exact content or absence, previous running/stopped state, observed previous command, and readiness timeout. Files are mode 0600 and the state directory is mode 0700. No audio, transcripts, environment dumps, journal output, or recognition history are stored.

It then writes only its drop-in, calls `systemctl --user daemon-reload`, and restarts the existing unit. Health requires the unit to be active, its main process command to exactly match the selected configuration, that process to own an IPv4 listening socket at `127.0.0.1:6006` or `0.0.0.0:6006`, and a successful IPv4 loopback WebSocket handshake with proxies and redirects disabled. Other addresses and IPv6-only ownership are rejected because the IPv6 socket table cannot prove dual-stack support for the probe. It rechecks the main process afterward. A different listener alone cannot pass. This verifies process/transport readiness, not acoustic accuracy or resource consumption.

On success the pending snapshot becomes `recovery.json`, the last activation's recovery point. Reapplying the same managed command when healthy performs only health checks and preserves that point. On failure the manager restores the exact old managed content, or removes only its own newly created file, reloads, and restores the previous running/stopped state. A previously running service must pass its previous-command health check. A previously stopped or failed service is left stopped; systemd failure-result metadata is not recreated.

Failed attempts preserve an older successful recovery point. If snapshot promotion already consumed `pending.json`, recovery recreates it first and restores the older recovery point before attempting service recovery. A failed service restart or readiness check therefore cannot discard the older baseline. If automatic recovery also fails, both errors are reported and `pending.json` is retained. New applies are blocked until `rollback` retries that snapshot. Explicit rollback prefers an unfinished pending snapshot, otherwise the last successful recovery point. It works even if the current ASR TOML is broken or weights are missing. It restores service configuration and verifies prior running-state health, without deleting any model. A successful explicit rollback retains the successful recovery point for repeated recovery.

Ordinary exceptions and SIGINT/SIGTERM/SIGHUP trigger recovery during activation. Further signals are ignored while bounded recovery runs. SIGKILL, power loss, filesystem failure, or external edits cannot be made perfectly transactional. Atomic writes and fsync reduce exposure, and the durable pending snapshot supports explicit recovery after interruption. If the recovery files themselves cannot be read, written, or synced, snapshot retention cannot be guaranteed. Do not manually delete pending state to bypass an unresolved activation. The lock and path protections assume other processes running as the same user are trusted.

Rollback does not change `active_model` in TOML. After recovery, edit the desired selection back to v3 before a later apply if you intend to stay on v3. If the base unit or unrelated overrides changed since the snapshot, rollback cannot reconstruct them; an unexpected command or failed readiness check is reported. Keep those external files under separate control.

## Resource and network limits

The same 0.6B parameter count does not promise the same RAM, initialization time, recognition speed, or quality. The observed warm v3 server used about 689 MiB RSS before this work. A resident CPU model consumes memory while idle. Measure a separately validated Ultra export before making a resource claim.

The native server's existing wildcard listener and ineffective user-unit IP firewall directives are unchanged. A loopback client connection is not evidence of off-host isolation. See [server setup and exposure](server-setup.md#network-isolation). No UFW edit, namespace migration, or extra inference server is part of model selection.

## Removal

The desktop client and model manager have separate command links. Inspect `readlink ~/.local/bin/local-dictate-model`, then remove only that link if it points to this checkout and you no longer want the manager. Keep the checkout while either command still links to it.

The running service executes the native binary directly, not this Python manager, so removing the manager command does not stop ASR. Leave its managed drop-in in place if you want the current Ultra selection to survive future service restarts. Preserve `asr.toml`, recovery files, and both model directories unless deliberately retiring them. If you want to return to the original v3 service instead, run `rollback` and verify actual v3 before removing the manager command. Then change the desired TOML selection to v3 so it does not unexpectedly switch back on a later apply.

Do not remove the whole service, use `systemctl revert`, delete unrelated drop-ins, or erase the shared v3 directory as part of removing this command. A base-unit or model path needed by Pi must remain available. See [the actual activation and rollback record](ULTRA-ACTIVATION.md).

## Development verification

```bash
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -p 'test_models*.py' -v
bash -n scripts/install-model-manager.sh
sh -n bin/local-dictate-model
```

These tests use mocked service/probe calls, non-model fixture files, and temporary HOME/runtime directories. They cover validation, argument quoting, read-only preview, busy sessions, conflicting files, private paths, concurrent model operations, successful activation, idempotence, failure/interruption recovery, preservation of earlier recovery, and stopped-state restoration. They do not activate the real service, download weights, record a microphone, type, change the clipboard, or send notifications.
