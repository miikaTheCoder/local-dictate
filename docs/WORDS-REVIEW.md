# Personal correction feature review

Recorded 2026-10-24 after the historical 170-test client/model council. This is a separate feature review, not a second three-reviewer council vote.

**APPROVE WITH CAVEATS.** The independent re-review closed the only confirmed finding. The final suite contains **221 passing tests**, retaining all 170 baseline tests and adding 51 correction-feature tests. Actual Zenity interaction and microphone-to-application delivery remain unverified.

## Scope and review rounds

The feature adds `local-dictate words add/list/remove/enable/disable/check/test/path/gui`, private `words.toml` persistence, and shared post-ASR corrections for desktop sessions and WAV stdout. It does not change the model, shared server, Pi Alt+M extension, hotkeys, or firewall. See [WORDS.md](WORDS.md) for matching semantics, limits, permissions, and usage.

Implementation and a separate behavioral audit preceded two independent GPT-6 Astra review tasks with xhigh reasoning and priority service tier configured:

1. [Round 1](t3-thread://v1/thread:delegated-task:command%3Amcp%3Afd1cf63c-c329-4453-aa78-ac5630804619%3Adelegate-task%3Alocal-dictate-word-rules-review-r1): REQUEST CHANGES. One P2 compatibility defect: an absent dictionary still caused the strict parent-directory open to reject a config beneath a symlinked directory. No other actionable defect was confirmed. The reviewer ran 218 tests in 8.673 seconds, 26,502 single-codepoint canonical-equivalence probes, and TOML round-trips for 154,809 accepted Unicode code points. Injected pre-commit ENOSPC preserved the original dictionary and removed temporary output. A mocked malformed GUI overwrite confirmation preserved dictionary bytes.
2. [Round 2](t3-thread://v1/thread:delegated-task:command%3Amcp%3Afd1cf63c-c329-4453-aa78-ac5630804619%3Adelegate-task%3Alocal-dictate-word-rules-review-r2): APPROVE WITH CAVEATS. R1P2 closed, no new actionable findings. All 221 tests passed in 9.218 seconds. Nine additional temporary-storage probes passed in 0.006 seconds, covering missing and unsafe paths, preflight errors, deletion races, late file swaps, parent-directory swaps, and mutation-path independence. The reviewer verified the checkout was unchanged by its work.

The fix uses `lstat()` to identify a genuinely absent optional dictionary before imposing new directory requirements. A dangling dictionary symlink is not treated as missing. Existing dictionaries still undergo strict directory/file validation; all mutations retain their original strict path. Three regressions cover absent dictionaries through symlinked directories, refusal to mutate through those directories, dangling dictionary links, new-session configuration, and mocked WAV stdout. Before the fix, two of the three tests reproduced the failure. Afterward, the coordinator's full run passed all 221 tests in 8.417 seconds.

## Coordinator integration checks

The installed command was tested with a private temporary dictionary, never the user's personal dictionary:

- Case-insensitive `Orange` to `Omarchy` matching worked, while `oranges` stayed unchanged.
- With `Orange` to `Omarchy` and `Omarchy` to `Linux`, preview returned `Omarchy and Linux` for `Orange and Omarchy`, proving one-pass behavior.
- Disable, enable, and remove worked. Dictionary and lock files were mode 0600.
- The bundled public English WAV was resampled to 16 kHz in private temporary runtime storage and transcribed through the already running native Ultra server. A temporary `country` to `Omarchy` rule produced the exact expected corrected stdout. The temporary audio and dictionary were removed afterward.
- The native service remained active with the same observed PID, 1070288. No restart or additional server was needed.
- `zenity --help-forms` advertised the required form/entry/separator flags. This was a help-only invocation, not an actual dialog test.
- At closeout, the installed `words path` selected `/home/nextg/.config/local-dictate/words.toml`, and `words check` reported zero rules. No personal correction was installed automatically.

No automated check recorded the microphone, typed into applications, changed the clipboard, or displayed notifications. GUI tests mocked Zenity. Reviewers used synthetic audio, fake local WebSocket servers, and private disposable files. Neither the feature nor its reviews changed live user configuration, service state, packages, hotkeys, Pi, or Git contents. The checkout has initialized Git metadata but all project source is untracked; no commits were made.

## Reviewed source fingerprints

SHA-256 after the compatibility fix and independent re-review:

```text
3e126b4557523e1a536ee9c6aa9afd7202792b1ac58d8f7ce583618bf3d8c9f2  word_rules.py
cfe5fadb1e9f4954507b95fd49cee10d454c6a0fcf6a2a7b6685dd7fa684d637  local_dictate.py
a271dca9713e232a94a37fbb7629895e0d68cb498b3c6cef1317140195090dca  tests/test_words.py
011234ca550e5c38b3f1727c3db1940ced2a2efeb46db086dc350f7724a0c189  tests/test_words_client.py
```

## Acceptance boundaries

- A real user still needs to exercise the add dialog and actual dictation/insertion. Automated success does not prove focus, widget acceptance, notification display, microphone permissions, or physical hotkeys.
- Literal corrections cannot distinguish meanings. An `Orange` rule also replaces the fruit. Rules affect the next desktop session and this client's WAV command, not Pi's separate client.
- Management arguments and intentional `list`/`test` output can appear in terminal or shell history. Preview accepts stdin; the dialog avoids passing rule contents in helper arguments. Existing clipboard-history implications remain unchanged.
- Manual file editors do not participate in the CLI lock. Detected concurrent changes are rejected, but arbitrary same-user writes at the final instant cannot be perfectly transactional.
- The [historical council caveats](COUNCIL.md#remaining-caveats), including dependency-version coverage and unverified off-host server isolation, remain in force. No network hardening was claimed or attempted for this feature.
