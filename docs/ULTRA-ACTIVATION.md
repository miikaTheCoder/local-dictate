# Ultra activation and rollback verification

Recorded 2026-10-24. The pinned Ultra export is now selected and running through the existing native CPU WebSocket service. This records actual parent-run integration checks, not a prediction from unit tests.

## Current configuration

- Desired selector: `/home/nextg/.config/local-dictate/asr.toml`, `active_model = "parakeet-ultra-int8"`.
- Ultra profile: `/home/nextg/.local/share/local-dictate/models/parakeet-ultra-int8-f7bc472677ff`, externally validated with `feat_dim = 128` and `model_type = "nemo_transducer"`.
- Native server remains sherpa-onnx 1.13.8, provider CPU, port 6006, four work threads and eight inference threads. No runtime upgrade, GPU setup, or second listening server.
- Managed override: `~/.config/systemd/user/sherpa-onnx-asr.service.d/60-local-dictate-model.conf`.
- Original v3 model and original base unit remain intact. The base-unit SHA-256 was `b4c2df2446c00b2cce767c97c46cf06a4cba62633c482204e11416584ef94f20` before and after activation.
- The managed command disables native argument printing and writes the native log to `/dev/null`. Old pre-existing service logs were not removed. The user journal may still contain service diagnostics.
- No UFW rules, Pi extension, or Nimble bindings changed. The original server-exposure caveat remains.

## Executed sequence

1. Independently recomputed sizes and SHA-256 hashes of all four Ultra files against the pinned staging manifest. All matched. The staged and project manifests are byte-identical.
2. Ran `local-dictate-model check --model parakeet-ultra-int8`, then selected Ultra and ran `apply`. The manager verified the expected process command, ownership of an IPv4 loopback-capable port-6006 listener, and a loopback WebSocket handshake.
3. Transcribed the bundled public English sample through the new live Ultra server.
4. Ran `rollback`. `list` showed desired Ultra but actual v3, as designed. Transcribed the same English sample successfully on v3.
5. Ran `apply` again. `list` showed desired and actual Ultra. Transcribed all four bundled public samples through the live server.
6. Repeated `apply` once more. It reported already applied and healthy, preserved PID 1070288, and left the recovery snapshot hash unchanged. This reapply did not restart the service.
7. `local-dictate status --json` reported idle. `doctor` passed. Hyprland `configerrors` remained empty.

The three deliberate model-transition restarts used these processes. `NRestarts=0` below means zero automatic systemd failure restarts, not that no deliberate restart occurred:

| Phase | Main PID | Active | Automatic restarts | RSS KiB | Cgroup MemoryCurrent bytes |
|---|---:|---|---:|---:|---:|
| First Ultra apply, after EN | 1064580 | yes | 0 | 1115208 | 1135259648 |
| Verified v3 rollback, after EN | 1070123 | yes | 0 | 1051924 | 1059131392 |
| Final Ultra, after EN/DE/ES/FR | 1070288 | yes | 0 | 1121648 | 1129828352 |

Final observed Ultra RSS was about 1095 MiB, or 1.07 GiB. All observed server processes had 26 OS threads. These samples are not a maximum memory bound or a controlled performance comparison. Workload length and inference buffers affect RSS.

## Live WebSocket sample results

The model's bundled public WAVs were resampled with the already-installed ffmpeg into a private temporary runtime directory, to PCM16 mono 16000 Hz for `local-dictate transcribe`. The directory was removed afterward. No microphone or user's recording was used. No text was injected and the clipboard was not changed.

CLI wall time includes starting the Python client, WebSocket upload, native recognition, and result output. It excludes audio capture duration and ffmpeg resampling:

| Sample | First Ultra | v3 rollback | Final Ultra |
|---|---:|---:|---:|
| EN, 3.845 s audio | 0.402 s | 0.393 s | 0.343 s |
| DE, 2.752 s audio | not run | not run | 0.352 s |
| ES, 5.329 s audio | not run | not run | 0.457 s |
| FR, 4.969 s audio | not run | not run | 0.471 s |

Final Ultra transcripts:

- EN: Ask not what your country can do for you, ask what you can do for your country.
- DE: Alles hat ein Ende, nur die Wurst hat zwei.
- ES: No preguntes ¿qué puede hacer tu país por ti, pregunta qué puedes hacer tú por tu país.
- FR: Ne vous demandez pas ce que votre pays peut faire pour vous, demandez-vous plutôt ce que vous pouvez faire pour lui.

The Spanish punctuation differs from the one-shot native-CLI test. These four short samples demonstrate native WebSocket compatibility and usable output, not superior accuracy or a reproduction of Moondream's WER results. See [the source evaluation caveats](ULTRA-PROVENANCE.md).

## Software checks and remaining acceptance

After the fixes and activation, `/usr/bin/python3 -m unittest discover -s tests -q` passed **170 tests in 8.152 seconds**: 132 client/setup tests and 38 model-manager tests. Model-manager regressions cover preserving earlier recovery during combined failures, exact IPv4 endpoint ownership, and total health deadlines. Client regressions include redirect rejection, failed state publication, notification-independent capture, late-cancel delivery outcomes, and cancellation-safe helper reaping.

The user still needs to exercise actual microphone capture, physical Alt+C/Alt+Shift+C interaction, Unicode typing into their applications, clipboard fallback, and visible beta notifications. Notification replacement depends on desktop support. Cancellation cannot retract already delivered text. The app cannot detect whether a focused widget accepts text. CPU int8 WER, long/noisy speech, and off-host isolation were not measured.

The final council reviewed this completed activation and returned [APPROVE WITH CAVEATS, 3/3](COUNCIL.md). A passing smoke test or review is not a claim of production stability.
