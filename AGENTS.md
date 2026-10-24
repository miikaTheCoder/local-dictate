# Local Dictate

Personal, lightweight system-wide dictation for Omarchy / Hyprland. Keep the CPU sherpa-onnx offline WebSocket architecture. The original baseline is NVIDIA Parakeet TDT 0.6B v3 int8. The user has now explicitly requested evaluating and upgrading to Moondream Parakeet Ultra at the same 0.6B parameter count with a compatible int8 export. Verify provenance, native compatibility, resource use, and recognition before switching; preserve v3 for rollback. No cloud inference, GPU, or unnecessary duplicate running servers.

## Working rules

- Read README.md and relevant code before changes. Do not use Unicode em dashes in new prose or comments.
- Keep runtime dependencies small. The desktop client must have no persistent idle daemon and no ASR inference code.
- Never record the user's microphone, type into an application, or change the clipboard as an automated test. Use mocks, synthetic audio, and explicitly requested interactive checks.
- Audio stays in bounded memory or private runtime storage and is discarded. Do not log transcripts or retain history by default.
- Do not install packages, modify live user configuration, or change service state from delegated work unless explicitly tasked. Never edit packaged Omarchy files.
- Preserve the existing Pi extension, Nimble bindings, other user settings, and baseline server/model files. A verified Ultra upgrade should use a reversible user-service drop-in rather than overwrite the original unit or delete v3. Only the coordinating agent may activate the upgrade unless explicitly delegated.
- Tests must cover concurrency/cancellation, protocol framing, and failure paths, not just a happy path.

## Implementation contract

- Launcher: bin/local-dictate. Commands: toggle, start, stop, cancel, status (with --json), doctor, transcribe WAV (stdout only, no typing), and words subcommands (see docs/WORDS.md).
- Configuration: ~/.config/local-dictate/config.toml, example in config.example.toml.
- Runtime: a private directory under XDG_RUNTIME_DIR. No persistent desktop daemon while idle.
- Desktop hotkeys: Alt+C toggle on release, Alt+Shift+C cancel.
- ASR URL: ws://127.0.0.1:6006, loopback only, WebSocket proxying disabled.
- Capture: parec --raw --format=float32le --rate=16000 --channels=1, using PipeWire's PulseAudio compatibility service. This matches the installed Pi client.
- Protocol: int32 little-endian sample rate and sample byte length, then float32 little-endian mono PCM frames. Receive JSON with text, then send text Done. Done is not the audio terminator.
- Insertion: wtype via stdin, with wl-copy fallback. No shell interpolation, no automatic Enter or paste chord. Explain clipboard-history implications.

## Existing machine reference

- Pi source: /home/nextg/Work/pi-dictate-local, commit 552eb43efd86406772dc9bd5da7376b69a4067ef.
- Existing enabled user unit: sherpa-onnx-asr.service.
- Unit source: /home/nextg/Work/pi-dictate-local-runtime/sherpa-onnx-asr.service.
- Binary: /home/nextg/Work/pi-dictate-local-runtime/server/sherpa-onnx-v1.13.8-linux-x64-static/bin/sherpa-onnx-offline-websocket-server.
- Model: /home/nextg/Work/pi-dictate-local-runtime/models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.
- Existing user-unit IPAddressDeny=any and IPAddressAllow=localhost are not enforced, confirmed by the user-manager journal warning that it is not running as root. The native server listens on 0.0.0.0. UFW is active, but effective off-host isolation is unverified. Leave the existing service and firewall unchanged for client setup; document optional hardening without claiming loopback-only binding.
- Existing footprint before changes: 705152 KiB RSS, about 689 MiB, and negligible sampled idle CPU. A warm 0.6B model does not use zero RAM.
- User Hyprland config uses Lua, ~/.config/hypr/bindings.lua. Legacy .conf examples are secondary.

## Model-switch workflow for future agents

- Installed control file: ~/.config/local-dictate/asr.toml. Desktop settings remain in config.toml. Model-manager commands are separate: local-dictate-model list/check/apply/rollback.
- Ultra is now active, from the pinned leogaudin export at revision f7bc472677ff70be95cdb0ef9df509fee6f5ec0b. The preserved v3 profile remains available. Read docs/MODELS.md, docs/ULTRA-NATIVE-VALIDATION.md, and docs/ULTRA-ACTIVATION.md before changing the model.
- Edit active_model, run check, and explicitly apply only when switching is authorized and both desktop and Pi dictation are idle. Editing TOML alone does not restart anything. The same shared CPU server and ws://127.0.0.1:6006 endpoint stay in use.
- Validate any new export independently before setting its validated field. Check only verifies files and flags, not model quality. Never infer int8 WER from full-precision publisher scores.
- The manager owns only 60-local-dictate-model.conf and private recovery records. Preserve the original unit, both models, and unrelated drop-ins. Rollback restores the prior service state but does not edit the desired active_model value.
- Temporary beta notifications are enabled. Once the user is satisfied with stability, disable them with notifications = false in the desktop config rather than remove status/error logic.
- Historical client/model council verdict: APPROVE WITH CAVEATS, 3/3, at the 170-test baseline. Read docs/COUNCIL.md for closed findings and remaining acceptance boundaries. The tested WebSocket dependency is 17.0.1; rerun the full suite, especially localhost/redirect checks, when upgrading dependencies.

## Personal correction workflow for future agents

- Read docs/WORDS.md before changing corrections. Use local-dictate words add/list/remove/enable/disable/check/test/path/gui or edit private ~/.config/local-dictate/words.toml. With --config, the dictionary is the selected config's sibling words.toml.
- Corrections are deterministic post-ASR whole-word/phrase replacements for Alt+C and this client's transcribe command, not model training or changes to Pi Alt+M. Dictionary edits affect the next session; no service restart is needed. Never install illustrative rules without the user's deliberate choice.
- Preserve case/canonical matching, longest-at-position single-pass behavior, private atomic updates, pre-capture validation, and recovery commands with malformed dictionaries. A missing dictionary must remain a no-op, including below symlinked config directories; existing dictionaries and mutations retain strict checks.
- The separate feature re-review approved with caveats after 221 tests passed. See docs/WORDS-REVIEW.md. Real Zenity interaction and microphone-to-application delivery remain manual acceptance checks; never exercise these as automated tests.
