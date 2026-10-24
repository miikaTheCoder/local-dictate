# Parakeet Ultra native CPU validation

Pre-activation validation snapshot, recorded on 2026-10-24. At the time of
these tests, Ultra was staged but not activated, and the shared v3 service
remained active with PID `1987` and the same start timestamp. Ultra was later
activated successfully, with a verified v3 rollback and Ultra reapply. See
[ULTRA-ACTIVATION.md](ULTRA-ACTIVATION.md) for that later record and use
`local-dictate-model list` for current desired and actual state. The historical
measurements and staging manifest below are preserved unchanged.

The staged directory is:

```text
/home/nextg/.local/share/local-dictate/models/parakeet-ultra-int8-f7bc472677ff
```

The project record is
[`models/ultra-f7bc472677ff.manifest.json`](../models/ultra-f7bc472677ff.manifest.json).
It is byte-identical to `manifest.json` inside the staged directory. Its SHA-256
is `03c1d0c9390d3f3747a0a9ca655f4d00d5aa18ca7370ce6183b7f4fde9620092`.
The manifest includes exact pinned HTTPS download URLs, file sizes and hashes,
provenance evidence hashes, native argument arrays, fixture hashes, transcripts,
resource measurements, and service identity snapshots.

The export is
[`leogaudin/sherpa-onnx-nemo-parakeet-ultra-int8`](https://huggingface.co/leogaudin/sherpa-onnx-nemo-parakeet-ultra-int8/tree/f7bc472677ff70be95cdb0ef9df509fee6f5ec0b),
pinned to `f7bc472677ff70be95cdb0ef9df509fee6f5ec0b`. Small metadata was obtained
and audited before downloading weights: its model card, export script,
`SHA256SUMS`, and revision-specific Hugging Face API metadata with LFS hashes.

| File | Exact bytes | SHA-256 |
|---|---:|---|
| `encoder.int8.onnx` | 652282340 | `8bd3b8dc07056def6dfd19910e9f90c9e1a4e7e20901f58bdfe2a570a8081a2a` |
| `decoder.int8.onnx` | 11845275 | `94373c6405c8149d1c9d1e9862514e0e3e88dab1c7de18fac9d5df9c6ba5efdd` |
| `joiner.int8.onnx` | 6355277 | `8a71aaccfdba3d451775507a889afd2257d2e754463fd81095a8c8f290f924c2` |
| `tokens.txt` | 93939 | `d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d` |

Total model size: **670576831 bytes, 639.512 MiB**. All four downloaded sizes and
SHA-256 values match the pinned published checksums. The three ONNX hashes also
match the API's LFS SHA-256 values. `tokens.txt` is a regular Git blob, not an
LFS object; its Git blob ID `fc43e1c723e262df60b70e1919614417162d1fe2` also
matches. Its SHA-256 is identical to the existing local v3 `tokens.txt`.
All four model hashes were checked again after native validation.

The export script is retained as inert text at
`provenance/export_ultra.py.txt`. Its SHA-256,
`bdb071a788ef7a3d65791da40ad2b26c1e65f32fa75f1155c63741370058c2d6`, matches
the pinned `SHA256SUMS`. The model card, checksums, and script also match the
sizes and Git blob IDs in the revision-specific API response. This verifies
consistency with the pinned publisher's bytes; these checks are not signatures.
The export script was never executed and no conversion dependency was installed.

The audited upstream source snapshot is
[`moondream/parakeet-ultra` at `73175eb7aeb0d82f1e2a6b53b3aabc10a90bcd0b`](https://huggingface.co/moondream/parakeet-ultra/tree/73175eb7aeb0d82f1e2a6b53b3aabc10a90bcd0b).
Its official card describes a post-trained derivative of NVIDIA Parakeet TDT
0.6B v3 with the same recognition architecture and tokenizer, plus a small
voice activity head. The pinned API reports 627270663 source parameters,
including that head, consistent with the 0.6B class. The source card and
configuration were checked against that revision's Git blob metadata and
retained under `provenance/`; the source weights were not downloaded.

The community converter documents mapping 723 recognition tensors into NeMo
with checks for missing, unexpected, duplicated, and differently shaped weights.
Its script checks tokenizer ordering, omits the six `vad_head.*` tensors, exports
the recognition branches, and uses dynamic quantization with unsigned encoder
weights and signed decoder/joiner weights. Those statements are supported by
reading the script, but this conversion was not independently reproduced.
In particular, the script's default `hf_hub_download` calls do not specify source
revisions. The exact export bytes are pinned here; the original input revisions
used by its publisher are not embedded or independently proven. No alternate
export candidate was substituted.

Credit belongs to Moondream for Ultra weights, NVIDIA for the Parakeet v3
architecture, tokenizer and base weights, leogaudin for this community conversion,
and sherpa-onnx for the export recipe and native runtime. The source and export
cards identify the model license as [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
The identified changes are the Transformers-to-NeMo mapping, ONNX export, int8
quantization, omitted auxiliary VAD head, and a metadata URL containing lowercase
`tdt`. The runtime is Apache-2.0. No NVIDIA or Moondream endorsement is implied.

The existing native runtime was used on this machine's AMD Ryzen 5 5600X CPU:

```text
sherpa-onnx version : 1.13.8
sherpa-onnx Git SHA1: 11afbd00
sherpa-onnx Git date: Thu Sep 10 13:52:45 2026
onnxruntime version : 1.28.2
```

Executable:

```text
/home/nextg/Work/pi-dictate-local-runtime/server/sherpa-onnx-v1.13.8-linux-x64-static/bin/sherpa-onnx-offline
```

Executable SHA-256:
`db374bfbe8d10626181c583efe05fb60f28eb4dd1f3f79686024a77bd41748ca`.
Version information came from the sibling `sherpa-onnx-version` executable.
**No newer runtime is required for the tested offline CLI path.** Compatibility
with earlier versions was not established. The offline CLI's help was read
before testing. It omits `nemo_transducer` from its listed model types, but the
installed executable accepts that value and the successful tests confirm it.

The tested arguments were:

```bash
ultra_dir=/home/nextg/.local/share/local-dictate/models/parakeet-ultra-int8-f7bc472677ff
fixture_dir=/home/nextg/Work/pi-dictate-local-runtime/models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/test_wavs
/home/nextg/Work/pi-dictate-local-runtime/server/sherpa-onnx-v1.13.8-linux-x64-static/bin/sherpa-onnx-offline \
  --provider=cpu --num-threads=8 \
  --model-type=nemo_transducer --feat-dim=128 \
  --sample-rate=16000 --decoding-method=greedy_search --debug=false \
  --encoder="$ultra_dir/encoder.int8.onnx" \
  --decoder="$ultra_dir/decoder.int8.onnx" \
  --joiner="$ultra_dir/joiner.int8.onnx" \
  --tokens="$ultra_dir/tokens.txt" \
  "$fixture_dir/en.wav"
```

This shows the executable arguments, not the test watchdog. The actual harness
bounded each direct native child to 120 seconds, sent SIGTERM to its dedicated
process group on expiry, and allowed five seconds before SIGKILL. It set only
that child's core-dump soft and hard limits to zero. All four children exited
normally with code zero; none reached the timeout. English used `--debug=true`
to inspect public fixture metadata; the other languages used `--debug=false`.

Native startup reported these encoder metadata values:

```text
version=2
model_author=NeMo
model_type=EncDecRNNTBPEModel
feat_dim=128
url=https://huggingface.co/moondream/parakeet-ultra (tdt, trained from nvidia/parakeet-tdt-0.6b-v3)
subsampling_factor=8
pred_hidden=640
pred_rnn_layers=2
normalize_type=per_feature
vocab_size=8192
comment=Only the transducer branch is exported
```

The native joiner initialization explicitly reported
`TDT model. vocab_size: 8193, num_durations: 5`. The encoder vocabulary count
excludes the blank token; the joiner count includes it. The URL's lowercase
`tdt` marker is present, allowing this runtime to recognize duration outputs.
Keep the original metadata and explicitly pass `--feat-dim=128`.

All fixtures were existing public PCM16 mono WAV files. English is 24000 Hz;
German, Spanish, and French are 22050 Hz. The native logs confirmed internal
resampling to 16000 Hz. No audio file was modified or copied.

| Fixture | Audio seconds | Recognizer creation s | Decode s | Native decode RTF | Whole process s | Peak RSS KiB | Peak RSS MiB |
|---|---:|---:|---:|---:|---:|---:|---:|
| EN | 3.845083 | 1.627 | 0.250 | 0.065 | 1.949353 | 1073448 | 1048.289 |
| DE | 2.751565 | 1.609 | 0.172 | 0.063 | 1.857873 | 1100668 | 1074.871 |
| ES | 5.328980 | 1.587 | 0.256 | 0.048 | 1.917133 | 1096984 | 1071.273 |
| FR | 4.969070 | 1.586 | 0.258 | 0.052 | 1.917784 | 1108124 | 1082.152 |

The requested `/usr/bin/time -v` measurement could not run because GNU time is
not installed; no existing `/bin/time`, `/usr/local/bin/time`, `gtime`, or user
copy was found. The initial wrapper exited 127 before launching the native
executable. That prerequisite failure is retained in
`validation/time-prerequisite-attempt.json`. No package was installed.

The successful tests instead used Linux `os.wait4` directly on each native child
to obtain actual `ru_maxrss` in KiB, user CPU time and system CPU time. Whole
process duration used `time.monotonic`; recognizer creation, decode time and RTF
came from native stderr. These are measured resource counters, not estimates,
but they are not GNU time output. Exact CPU and additional resource counters are
saved in `validation/{en,de,es,fr}.resources.json` and the manifest.

The public fixture transcripts were:

- EN: Ask not what your country can do for you, ask what you can do for your country.
- DE: Alles hat ein Ende, nur die Wurst hat zwei.
- ES: No preguntes qué puede hacer tu país por ti, pregunta qué puedes hacer tú por tu país.
- FR: Ne vous demandez pas ce que votre pays peut faire pour vous, demandez-vous plutôt ce que vous pouvez faire pour lui.

Each process created a fresh recognizer. The OS page cache was not flushed, so
these runs should not be described as disk-cold measurements. Their startup
costs are also not comparable to latency from the already warm v3 WebSocket
service. Peak RSS belongs only to the temporary Ultra CLI process. The existing
v3 process remained resident concurrently and was observed at 1721036 KiB RSS
before and after these tests. The CLI peaks do not establish the memory use of
a future persistent Ultra server or system-wide physical RAM consumption.

The download used a mode-0700 temporary sibling directory and mode-0600 files,
HTTPS-only redirects, exact expected sizes, and incremental SHA-256 verification.
It refused an existing destination, verified all four model files before
publication, flushed files to disk, and published with Linux
`renameat2(RENAME_NOREPLACE)`. The temporary sibling no longer exists. No
pre-existing directory was overwritten. The final model directory is private.

Final read-only service checks found:

```text
ActiveState=active
SubState=running
MainPID=1987
ExecMainStartTimestampMonotonic=41918053
process start: Fri Oct 23 19:22:55 2026
```

The existing `0.0.0.0:6006` listener still belonged to PID 1987. This validation
started no server or new network listener and did not inspect private service
logs. It performed no microphone capture, typing, clipboard changes,
notifications, privilege escalation, package installation, live configuration
changes, or service operations. Only the owned model staging artifacts, project
manifest, and this report were written, with necessary parent directories.

This is a native CPU compatibility and recognition smoke test. CPU int8 WER
was not measured here; four short clips do not establish better accuracy than
v3. The community card's benchmarks and the official Photon/GPU scores were
not reproduced. VAD segmentation, long/noisy audio, hotwords, and WebSocket
integration were not tested. The export omits the VAD head and `bpe.vocab`.
At the time of this snapshot, TOML apply/rollback, reversible service activation,
and WebSocket integration validation were still coordinating-agent work. Those
steps subsequently passed, as recorded in [ULTRA-ACTIVATION.md](ULTRA-ACTIVATION.md).
The original v3 files remain available for rollback.
