# Local Dictate

Lightweight, system-wide local dictation for **Linux with Omarchy / Hyprland**. Press **Alt+C** to start speaking and press it again to insert the transcript into the focused application. **Alt+Shift+C** cancels. Optional personal corrections help with recurring names and recognition mistakes.

Speech recognition runs on your CPU through a separate native **sherpa-onnx** server. Inference needs no cloud service, account, or GPU. The desktop client has no inference library and **no persistent idle daemon**. The ASR server stays resident and keeps the model in RAM while it is running.

**Personal beta:** tested on one Omarchy / Hyprland setup, with native Linux x86_64 server validation. Other Linux desktops, Windows, and macOS are not verified targets. Source reviews approved with caveats and the safe suite has 221 passing tests. Real microphone-to-application delivery, visible notifications, and the Zenity dialog remain manual acceptance checks.

## Quickstart

<a id="install-the-client-on-the-current-machine"></a>

Check the [prerequisites](#clean-omarchy--arch-installation). You need a Hyprland desktop session, working PipeWire PulseAudio compatibility, Python dependencies, and the desktop tools listed there. Clone into a permanent user-owned directory:

```bash
git clone https://github.com/miikaTheCoder/local-dictate.git
cd local-dictate
```

Choose the server setup:

- **Existing compatible Pi/sherpa server:** reuse its CPU service at `ws://127.0.0.1:6006`. Preserve its model, unit, and Pi settings. Do not start a duplicate server.
- **New installation:** follow [the separate native server setup](docs/server-setup.md#new-machine-downloads), including its network guidance and service setup, before continuing. It bootstraps **NVIDIA Parakeet TDT 0.6B v3 int8** with **sherpa-onnx 1.13.8**, not Ultra.

With the server ready, install the client from this checkout and inspect readiness:

```bash
bash scripts/install-client.sh
"$HOME/.local/bin/local-dictate" doctor
"$HOME/.local/bin/local-dictate" status --json
```

[Add the hotkeys manually](#add-the-hotkeys-manually), then try dictation in an editable field when you are ready to record.

The installer preflights dependencies, creates `~/.local/bin/local-dictate` as a symlink to this checkout, and copies desktop config only when absent. It preserves existing settings and refuses conflicting command files. It does **not** install dependencies, hotkeys, a server, models, or the separate model manager. Keep the checkout in place. Use the full command path above if `~/.local/bin` is not on `PATH`. `doctor` probes the server without recording, typing, or copying.

**Models and native binaries are not bundled.** Obtain them separately from upstream using the linked setup guide. The repository's `models/` directory contains only a small provenance/checksum JSON manifest, not weights. Keep downloaded models, servers, and private configuration outside the checkout.

**Network caveat:** the tested native server binds to `0.0.0.0:6006`. A loopback client URL and disabled proxies do not secure inbound access. The reference user unit's `IPAddressDeny` / `IPAddressAllow` directives were ineffective, and external isolation remains unverified. Read [network isolation](docs/server-setup.md#network-isolation) before treating the port as private.

## Credits and upstream components

Local Dictate is the desktop client and integration, not the author of the speech models. It is not endorsed by these upstream projects:

- **[NVIDIA](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)** created the Parakeet TDT 0.6B v3 base model.
- **[Moondream](https://huggingface.co/moondream/parakeet-ultra)** created Parakeet Ultra through post-training of that base model, retaining the 0.6B parameter count.
- **[leogaudin](https://huggingface.co/leogaudin/sherpa-onnx-nemo-parakeet-ultra-int8/tree/f7bc472677ff70be95cdb0ef9df509fee6f5ec0b)** provided the community sherpa-compatible ONNX int8 Ultra export tested here, pinned at `f7bc472677ff70be95cdb0ef9df509fee6f5ec0b`.
- **[k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)** provides the native CPU inference runtime, offline WebSocket protocol, and the [v3 ONNX int8 release](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models).
- **[K3ll3s/pi-dictate-local](https://github.com/K3ll3s/pi-dictate-local/tree/552eb43efd86406772dc9bd5da7376b69a4067ef)** and **[Amos Blomqvist's pi-dictate](https://github.com/amosblomqvist/pi-dictate)** supplied the reference client and protocol lineage.

The upstream models use [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), sherpa-onnx uses Apache-2.0, and the Pi reference code uses MIT. Those licenses govern their respective components. No license has been selected here for newly authored Local Dictate code. See [full attribution and license details](docs/ATTRIBUTION.md) and the retained [Pi MIT notice](docs/PI-DICTATE-LICENSE.txt).

## Clean Omarchy / Arch installation

Required client tools and libraries:

- **Python 3.11+** and **`websockets>=15`** with explicit proxy disabling support. The tested combination is **Python 3.14.8 / websockets 17.0.1**. The minimum does not promise coverage of every release; rerun the suite after dependency upgrades.
- **`parec`**, supplied by Arch's **`libpulse`**, and a working PulseAudio compatibility server such as **`pipewire-pulse`**. Inspect your existing audio stack before changing it.
- **`wtype`**, **`wl-copy`** from **`wl-clipboard`**, and **`hyprctl`** from Hyprland. The installer requires all three, including clipboard fallback support.
- Optional **`notify-send`** from **`libnotify`** for status feedback and **`zenity`** for the correction add form. Git is needed to clone; curl is used by the separate server download guide.

On a new Omarchy machine, install missing dependencies manually:

```bash
omarchy pkg add python python-websockets libpulse pipewire-pulse wtype wl-clipboard libnotify curl git
```

Then follow [native binary/model downloads and user-service setup](docs/server-setup.md#new-machine-downloads). Its checked archive extraction requires **Python 3.12+** and a few GiB of staging space. The guide pins native **sherpa-onnx 1.13.8** and the **v3 int8** model with checksums. No PyTorch, NeMo, NumPy, Python ASR engine, or GPU runtime is needed. Ultra is an optional, separately validated model path, not automatically provisioned by a clone or installer.

Prefer distro Python packages. If you choose an isolated client dependency environment, create it inside the permanent checkout before running the installer:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
bash scripts/install-client.sh
```

The launcher and installer choose `LOCAL_DICTATE_PYTHON` when explicitly set, otherwise checkout-local `.venv/bin/python`, otherwise `/usr/bin/python3`. The local venv works from hotkeys without shell activation. An interpreter override must also be available in the hotkey environment. No package installation is automated by the project.

## Add the hotkeys manually

The installer never edits Hyprland configuration. For Omarchy's Lua bindings, review existing shortcuts and back up `~/.config/hypr/bindings.lua`:

```bash
omarchy menu keybindings --print
cp -p -- "$HOME/.config/hypr/bindings.lua" "$HOME/.config/hypr/bindings.lua.bak.$(date +%Y%m%d-%H%M%S)"
```

Add these lines to the existing file, as shown in [the binding example](examples/hypr-bindings.lua):

```lua
o.bind("ALT + C", "Toggle Local Dictate", "$HOME/.local/bin/local-dictate toggle", { release = true })
o.bind("ALT + SHIFT + C", "Cancel Local Dictate", "$HOME/.local/bin/local-dictate cancel")
```

Review collisions before replacing any existing global binding. If appropriate, use `hl.unbind("ALT + C")` / `hl.unbind("ALT + SHIFT + C")` first. Preserve other bindings, including Pi Alt+M and any Nimble shortcuts. A global Alt+C consumes that shortcut in applications. Other Hyprland configuration formats need an equivalent manual binding.

Validate after editing:

```bash
hyprctl reload
hyprctl configerrors
```

Fix reported errors before use. Alt+C toggles on **C key release**, not push-to-talk. That does not guarantee Alt is released: release modifiers promptly after starting and stopping, and keep them released during delivery.

## Use and data handling

Focus an editable field, press and release Alt+C, speak, then press and release Alt+C again. Keep the intended field focused until completion. Alt+Shift+C cancels a recording or pending transcription. Beta notifications show recording, transcription, delivery, cancellation, or failure without including transcript text.

The same actions are available from the CLI:

```bash
local-dictate toggle
local-dictate start
local-dictate stop
local-dictate cancel
local-dictate status
local-dictate status --json
local-dictate doctor
local-dictate transcribe /absolute/path/to/existing.wav
```

`start` records the microphone; `stop` ends capture and transcribes. `transcribe WAV` reads an existing uncompressed **PCM16 WAV at 16000 Hz, mono or stereo**, within the configured duration limit. It writes text to stdout only, without recording, typing, or clipboard changes. It still requires the local ASR server.

Capture uses `parec` float32 mono PCM at 16000 Hz. Audio stays in bounded memory or private storage under `XDG_RUNTIME_DIR` and is discarded. The client keeps no audio, transcript history, or transcript logs by default. Existing server logs have their own policy; inspect them before sharing. The supplied new-machine service disables the native log file.

Text goes to `wtype` through stdin without shell interpolation, with `wl-copy` fallback when typing is unavailable, fails, or the focus guard detects a window change. No Enter or automatic paste chord is sent. Clipboard fallback replaces the current clipboard, and a clipboard manager may retain the transcript after this client exits.

The focus guard identifies a **window, not a text widget**. A successful `wtype` exit cannot prove the field accepted the text. Cancellation cannot retract text already typed or copied, including a partially delivered prefix. Check the target and delivery feedback before continuing.

The warm server consumes RAM even when the client is idle. Observed RSS was about **689 MiB for v3** and **1.07 GiB for Ultra after sample recognition** on the reference machine. These are observations, not memory or latency guarantees. Stopping a shared server also stops recognition for Pi and other clients.

## Configuration

Files normally live under `~/.config/local-dictate/`, or `$XDG_CONFIG_HOME/local-dictate/` when set:

- **`config.toml`** controls the desktop client: loopback `server_url`, optional audio `source`, recording/recognition limits, output mode, notifications, and insertion delay. See [config.example.toml](config.example.toml). Defaults include a 120-second capture limit, 30-second recognition timeout, and 300 ms insertion delay. Unknown keys are rejected. Use `output = "clipboard"` for manual paste, or `notifications = false` to disable beta feedback.
- **`words.toml`** contains optional private corrections. A missing file does nothing. Use the [words commands](#personal-word-corrections) to create and edit it safely.
- **`asr.toml`** belongs to the separate model manager, not the desktop client. **[asr.example.toml](asr.example.toml) contains reference-machine absolute paths. Adjust them before using it elsewhere.** Copying it does not provision models or switch the server.

Desktop settings and corrections are read for the next session without restarting ASR. `local-dictate --config /absolute/path/config.toml COMMAND` selects another desktop config and its sibling `words.toml`, not a new server or manager config.

## Personal word corrections

Illustrative example, to add deliberately and remove when finished:

```bash
local-dictate words add 'Orange' 'Omarchy'
local-dictate words test 'ORANGE and oranges'
# Omarchy and oranges
local-dictate words list
local-dictate words remove 'Orange'
```

**This also replaces references to actual orange fruit.** No example rule is installed automatically. Matching is literal, Unicode case-folded, and limited to whole words/phrases, with exact target capitalization. Longer matches win at the same position; replacements run once without cascading. This is post-ASR correction, not model training or hotword biasing.

Corrections affect Alt+C and this client's `transcribe WAV`, not Pi's separate Alt+M extension. `words list` and `words test` work safely offline without ASR or desktop actions, though they print your personal content to the terminal. CLI arguments may also appear in shell history and process listings.

`words enable`, `disable`, `check`, and `path` provide control and diagnostics. With optional Zenity installed, `local-dictate words gui` opens a two-field **Heard / Replace with** add form, then exits. Changing a target requires `--replace` on the CLI or GUI confirmation. The form is not a persistent dictionary manager. See [WORDS.md](docs/WORDS.md) for private file permissions, manual editing, matching details, and recovery.

## Model control

<a id="model-control-on-this-machine"></a>

The reference installation uses the pinned community **Ultra int8** export with the original **v3 int8** files preserved for rollback. This does not describe what a fresh clone installs. See [MODELS.md](docs/MODELS.md) before installing the separate manager or selecting a model.

After adjusting the manager's binary/model paths, its basic inspection commands are:

```bash
local-dictate-model list
local-dictate-model check
```

Editing `active_model` in `asr.toml` alone changes nothing in the running service. `check` verifies files and native flags, not recognition quality. Ultra needs independent provenance, native compatibility, resource, and recognition validation before marking its profile validated. Full-precision publisher WER scores are not verified int8 export results.

Only an explicit `local-dictate-model apply` activates the selection through a reversible user-service drop-in and **restarts the shared server**. Keep both desktop and Pi dictation idle; Pi activity cannot be detected automatically. Preserve the base unit, v3 model, and unrelated overrides. `local-dictate-model rollback` restores the prior service configuration but does not rewrite the desired TOML selection. The [native validation](docs/ULTRA-NATIVE-VALIDATION.md) and [activation/rollback report](docs/ULTRA-ACTIVATION.md) describe the tested upgrade and its limits.

## Troubleshooting, recovery, and removal

- **Silent hotkey or bad config:** run `local-dictate doctor` and `local-dictate status --json` in a terminal. Early config errors go to stderr. Compare settings with the examples; reinstalling preserves existing config.
- **Wrong source or no recording:** inspect `pactl info`, `pactl list short sources`, and `systemctl --user status pipewire-pulse.service`. Set the optional `source` using its PulseAudio name. Do not use unattended microphone capture as a diagnostic.
- **Connection refused or timeout:** inspect `systemctl --user status sherpa-onnx-asr.service`, its journal, and `ss -ltn '( sport = :6006 )'`. Check binary/model paths and avoid a second server. `0.0.0.0` is not loopback binding; see [network guidance](docs/server-setup.md#network-isolation).
- **Session stuck or corrections invalid:** use `local-dictate cancel`, `words check`, and the [dictionary recovery guide](docs/WORDS.md). Status/cancel remain available with malformed config. Do not delete active runtime files or kill unrelated Python/Pi processes.
- **Copied instead of typed:** check focus and `wtype` support, then paste manually into the intended field. A failed typing attempt can have delivered a prefix. Clipboard history may retain copies.
- **Command missing or broken link:** use the full `~/.local/bin/local-dictate` path and check the selected Python's `websockets` import. Keep the checkout at its installed location. Inspect an old link before removing it and reinstalling from a new path.
- **Binding errors:** restore your saved `bindings.lua`, reload, and check `hyprctl configerrors`. Preserve unrelated desktop settings.

To remove the client, cancel its session, remove only its two bindings, validate Hyprland, and remove its launcher symlink after verifying it points to this checkout. Review private config before deleting it. Keep the checkout while either launcher still links here. Follow [model-manager removal](docs/MODELS.md#removal) separately. Preserve shared ASR/Pi resources; [server lifecycle changes](docs/server-setup.md#manage-the-shared-service) are separate operations.

## Development checks

```bash
/usr/bin/python3 -m unittest discover -s tests -q
bash -n scripts/install-client.sh scripts/install-model-manager.sh
sh -n bin/local-dictate bin/local-dictate-model
```

The 221-test suite covers concurrency, cancellation, protocol framing, failure/recovery paths, dictionary behavior, and setup using synthetic audio, fake local WebSocket servers, temporary storage, and mocked desktop/service tools. It does not record the microphone, type, change the clipboard, launch a real GUI, or restart live services. Native model recognition and manual desktop acceptance are separate checks. Rerun the full suite, especially localhost/redirect checks, after dependency upgrades.

Further evidence and contributor guidance: [AGENTS.md](AGENTS.md), [verification record](docs/VERIFICATION.md), [historical client/model review](docs/COUNCIL.md), [correction feature review](docs/WORDS-REVIEW.md), and [Ultra provenance](docs/ULTRA-PROVENANCE.md).
