# Installation and verification

Recorded on 2026-10-24. This is a snapshot, not a guarantee about future runtime state. The initial client installation is recorded below. A later user-requested Ultra upgrade is now active, with a verified v3 rollback and Ultra reapply; see [ULTRA-ACTIVATION.md](ULTRA-ACTIVATION.md) for the current model, process, memory, and integration results.

## Installed on this machine

- Source: `/home/nextg/work/local-dictate`.
- Command: `/home/nextg/.local/bin/local-dictate`, symlink to this checkout's `bin/local-dictate`.
- Personal settings: `/home/nextg/.config/local-dictate/config.toml`, installed from the example with mode 0600.
- Hotkeys appended to `/home/nextg/.config/hypr/bindings.lua`: Alt+C toggle on C release, Alt+Shift+C cancel.
- Prior bindings saved as `/home/nextg/.config/hypr/bindings.lua.bak.local-dictate.20261024-162855`.
- During the initial client installation, Pi and Nimble shortcuts were preserved and no packages, models, ASR changes, restarts, or firewall changes were needed. The later explicitly requested Ultra upgrade downloaded one pinned model and deliberately restarted the same service through a reversible drop-in. The original unit, v3 files, Pi/Nimble bindings, and firewall are preserved.

## Observed checks

- `/usr/bin/python3 -m unittest discover -s tests -q`: after council fixes, 127 tests passed in 6.203 seconds. Python 3.14.8, websockets 17.0.1. Before those fixes, 113 tests passed in 5.315 seconds.
- `bash -n scripts/install-client.sh`, `sh -n bin/local-dictate`, and `luac -p examples/hypr-bindings.lua`: passed.
- Installed command's `doctor`: dependencies available, runtime private, session idle, loopback ASR WebSocket reachable with proxies disabled.
- Installed command's `status --json`: `{"state": "idle"}`.
- `hyprctl -i 0 reload`: `ok`. `hyprctl -i 0 configerrors`: empty, no reported errors.
- Running Hyprland bindings: Alt+C (modmask 8), release true; Alt+Shift+C (modmask 9), release false. Both descriptions confirmed.
- With the running Hyprland instance's environment, a read-only focus check connected to its event socket, obtained a focused window identity, and reported the focus guard ready. No window title or app content was logged.
- Actual native ASR integration: the model's bundled public `test_wavs/en.wav` is PCM16 mono at 24000 Hz. It was resampled to 16000 Hz in a temporary runtime directory using the already-installed ffmpeg, then passed to `local-dictate transcribe`. The temporary copy was removed afterward. ffmpeg is not a client runtime dependency.
- The native server returned: `Ask not what your country can do for you, ask what you can do for your country.` The complete CLI invocation took about 0.547 seconds for the 3.845-second sample. Repeating the same bundled-sample smoke test after the council fixes returned the same text in about 0.356 seconds. These are smoke tests, not a general latency benchmark.
- The existing server remained active under the same PID 1987 with zero service restarts. Before transcription its RSS was about 689 MiB; after native inference the observed RSS was 940524 KiB, about 918 MiB. Warm model and working buffers consume real RAM. No second model/server was launched.

## Review response before final council

The setup reviewer found one P2 bug: when `XDG_CONFIG_HOME` was set to an empty string, the installer used `~/.config` but the CLI used a relative path. The CLI now uses the same unset-or-empty fallback as the installer. Regression tests cover unset, empty, and nonempty XDG config roots and verify that clipboard-only settings are honored. The setup installer also has an empty-XDG test.

## Council round 1 and implemented responses

All three independent reviewers requested changes. Their required findings were:

- P1: a WebSocket HTTP redirect could escape the initial loopback URL validation. Connections now reject every redirect for both probing and transcription. Tests intercept unexpected transport attempts before any off-host I/O and assert no audio is sent after a redirect response.
- P2: a delayed Recording notification could postpone capture and lose initial speech. Capture now runs first, with readiness marked only after a complete PCM sample arrives. Background status notifications do not delay capture or decoding; synthetic tests cover blocked notifications and early stop.
- P2: failed runtime-state writes could prevent stop/cancel. Safety actions now take effect independently of best-effort state publication. ENOSPC regression tests cover stopping, cancellation, recorder cleanup, control replies, and prevention of transcript delivery.

The user requested temporary alpha/beta notifications. Session notifications now have a beta title and include cancellation. `notifications = false` disables all notification helpers. A replacement hint is supplied, but visible popup replacement is unverified. The nonblocking P3 observation that pre-session startup errors only reach stderr remains documented in the README.

Round 2 approved the privacy fixes with caveats and found two additional client P2 issues: late cancellation could misreport an already completed delivery, and cancellation during timed-out helper cleanup could interrupt reaping. The client now preserves completed delivery outcomes and shields bounded TERM/KILL reaping against repeated cancellation. Its 132 client/setup tests pass. A separate model-manager review found earlier-recovery preservation, exact listener-address verification, and deadline issues; those were fixed with regression tests. The full suite at that baseline passed 170 tests in 8.152 seconds. The historical final council review included these fixes and the completed Ultra activation. No earlier request-changes verdict is presented as approval.

## Personal correction feature follow-up

The CLI and optional Zenity add dialog are available through the existing launcher. The full suite now has 221 passing tests, retaining all 170 baseline tests. An independent re-review approved with caveats after closing a missing-dictionary/symlinked-config compatibility finding. Temporary-dictionary CLI round trips and native Ultra public-WAV transcription with corrections passed, without a service restart or installing a personal rule. Real dialog interaction and microphone-to-application delivery remain unverified. See [WORDS.md](WORDS.md) for usage and [WORDS-REVIEW.md](WORDS-REVIEW.md) for exact review evidence, test results, and source fingerprints.

## Still requires an interactive acceptance check

The agent did not record the user's microphone, type into an application, or modify the clipboard. Those effects were mocked in tests. In a scratch text field, the user should:

1. Press and release Alt+C, speak a short sentence, then press and release Alt+C again. Release Alt before delivery.
2. Confirm insertion in a terminal, browser text field, and editor. The app cannot detect whether a focused widget accepts text.
3. Begin another recording and press Alt+Shift+C. Confirm no insertion.
4. For an intentional clipboard check, set `output = "clipboard"`, dictate, and paste manually. This changes the clipboard and may enter clipboard-manager history. Restore `output = "auto"` afterward.

Live microphone source/permissions, actual Unicode typing, clipboard fallback, notification behavior, and physical hotkey interaction are not claimed as tested. Clean-machine installation and off-host network isolation are also not tested. The server's wildcard listener and ineffective user-unit IP firewall directives remain unchanged; see `server-setup.md` for optional hardening. This does not send client audio to the cloud.

The historical client/model council verdict was [APPROVE WITH CAVEATS, 3/3](COUNCIL.md). The later personal correction feature also received [approval with caveats](WORDS-REVIEW.md). Both retain the manual acceptance, dependency-version, model-quality, and network-posture limitations above.
