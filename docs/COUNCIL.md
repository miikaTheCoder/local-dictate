# Historical client/model council verdict

Recorded 2026-10-24 after client implementation, beta-notification fixes, model-manager fixes, native Ultra validation, live activation, v3 rollback, and Ultra reapply.

This snapshot predates the personal correction dictionary. Its separate later approval and 221-test results are recorded in [WORDS-REVIEW.md](WORDS-REVIEW.md); the baseline findings and fingerprints below remain historical.

**Unanimous APPROVE WITH CAVEATS, three of three independent review lanes.** No blocking defect remained in the final reviewed state. This is approval for a personal beta with the acceptance limits below, not a claim that every physical desktop interaction or future dependency version has been tested.

## Final reviews

- [Correctness and recovery](t3-thread://v1/thread:delegated-task:command%3Amcp%3Afd1cf63c-c329-4453-aa78-ac5630804619%3Adelegate-task%3Alocal-dictate-council-correctness-r3): APPROVE WITH CAVEATS. Independently ran all 170 tests, passing in 8.553 seconds, and inspected installed configuration, recovery state, and original-unit hash.
- [Privacy/security and Ultra provenance](t3-thread://v1/thread:delegated-task:command%3Amcp%3Afd1cf63c-c329-4453-aa78-ac5630804619%3Adelegate-task%3Alocal-dictate-council-safety-r3): APPROVE WITH CAVEATS. Ran 116 selected tests in 5.040 seconds with hostile proxy environment variables. Independently recomputed all four model hashes/sizes, checked retained provenance and manifests, and inspected the actual CPU process and listener.
- [Beta UX and config ownership](t3-thread://v1/thread:delegated-task:command%3Amcp%3Afd1cf63c-c329-4453-aa78-ac5630804619%3Adelegate-task%3Alocal-dictate-council-ownership-r3): APPROVE WITH CAVEATS. Independently ran all 170 tests in 8.152 seconds and inspected command links, selected model, and private configuration/drop-in permissions.

These were separate GPT-6 Astra review tasks with xhigh reasoning and priority service tier configured. At this baseline review, the directory had no Git metadata. The implementation fingerprints for that reviewed baseline were:

```text
b906eac554653592434cd8b7a508bd0a546301fac914ce8efe1440c8229199d8  local_dictate.py
716d12fa09a1a86ec58378f2764a49e5ea862a9f0b7525f46b3bc26829949714  model_manager.py
```

The coordinator's final read-only check found desired and actual Ultra, active PID 1070288, idle client status, successful loopback WebSocket health, and mode-0600 user-owned desktop config, ASR config, and managed drop-in. See [ULTRA-ACTIVATION.md](ULTRA-ACTIVATION.md) for live integration measurements and the exercised rollback sequence.

## Closed findings

- Empty `XDG_CONFIG_HOME` no longer causes installer/runtime config disagreement.
- HTTP redirects cannot bypass the validated loopback destination. Proxying remains disabled.
- Recording readiness follows actual PCM capture rather than preceding a blocking notification call.
- Failed state publication cannot disable stop/cancel or permit pending delivery after cancellation.
- Bounded child TERM/KILL reaping survives repeated cancellation, including cancellation after a notification timeout.
- Cancellation after completed delivery preserves the Inserted/Copied outcome instead of claiming the transcript was discarded.
- Failed model activation preserves earlier recovery even if promotion synchronization and service recovery both fail.
- Health checks verify ownership of the exact IPv4 loopback-capable endpoint, not an unrelated port-6006 listener.
- The health deadline is checked before every phase and before success.

The final UX reviewer found a nonblocking documentation ambiguity in the historical native-validation report. Its heading and closing notes now explicitly label it as the pre-activation snapshot and link to the later successful activation. Historical measurements and manifests were not rewritten.

## Remaining caveats

1. Physical Alt+C/Alt+Shift+C interaction, microphone source/permissions, Unicode insertion into real applications, clipboard fallback, and visible notification/replacement behavior still require user acceptance. Automated checks used synthetic audio and mocks; native integration used public bundled files. Focus detection is window-level, not editable-widget detection, and cannot atomically bind future keystrokes to a target. Cancellation cannot retract already delivered text.
2. The installed Python 3.14.8 and websockets 17.0.1 were tested. `requirements.txt` still permits `websockets>=15`; that open range is a minimum requirement, not an assertion that every past or future release was exercised. Rerun the full suite, especially locality/redirect/security regressions, before using a dependency upgrade.
3. Pre-session configuration/startup errors currently reach stderr rather than a desktop popup. Run `local-dictate doctor` when a hotkey appears silent.
4. The existing native server still has a wildcard listener. Its user-unit IP firewall directives are not enforced, and LAN/Tailscale isolation has not been tested. UFW was deliberately left unchanged. Client audio goes only to loopback, but that does not prove off-host clients cannot reach the server.
5. Ultra's pinned int8 export hashes and native compatibility are verified. Conversion was not reproduced, its publisher's source input revisions were not pinned in the script, and signatures/independent weight lineage were not established. CPU int8 WER and long/noisy-audio quality were not benchmarked. Publisher GPU benchmark figures are not this export's measured results.
6. Model switching is a controlled service restart. Keep both clients idle: Pi activity cannot be detected, and a new desktop session can race a previous idle check. Filesystem failure, SIGKILL, external edits, and power loss cannot be made perfectly transactional; durable recovery records and explicit rollback reduce those risks.

Temporary beta feedback remains enabled. Once the user is satisfied with stability, set `notifications = false` in `~/.config/local-dictate/config.toml`. Keep status and error handling available; no server restart or keybind change is needed.
