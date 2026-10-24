# Personal word corrections

Local Dictate can correct recurring recognition mistakes after transcription. This is a deterministic personal dictionary, not model training, hotword biasing, fuzzy matching, or a decoder change. It uses the existing local CPU server and adds no inference request, cloud service, package dependency, persistent process, or service restart.

Corrections apply to **Alt+C desktop dictation** and **`local-dictate transcribe WAV`**. They do **not** affect the separate Pi Alt+M extension, the shared ASR server, or its raw WebSocket results.

## Add, preview, and undo

These examples are illustrative. No example rule is installed automatically:

```bash
local-dictate words add 'Orange' 'Omarchy'
local-dictate words list
local-dictate words test 'ORANGE, oranges, and Orange.'
# Omarchy, oranges, and Omarchy.
printf '%s\n' 'Orange' | local-dictate words test
local-dictate words check
local-dictate words path
local-dictate words disable
local-dictate words enable
local-dictate words remove 'Orange'
```

**An `Orange` rule also changes references to the actual orange fruit.** The corrector has no semantic context. Use a longer phrase if that narrows unwanted matches, preview it, or remove/disable the rule. Disabled rules remain in the file and can be re-enabled. A missing file is an empty, enabled no-op dictionary.

Adding the same source and target again is idempotent. A different target requires an explicit overwrite:

```bash
local-dictate words add 'Orange' 'Another target' --replace
```

Removal uses the same case and canonical equivalence as matching, so `words remove 'ORANGE'` removes an `Orange` rule. Removing an absent rule succeeds without changing other rules.

`words test` accepts one quoted text argument, or bounded text from stdin, and prints only the resulting preview. It never starts ASR, captures audio, types, copies text, or sends a notification. Dictionary commands work without a desktop session, `XDG_RUNTIME_DIR`, or an ASR connection. `words list` and `words test` intentionally print personal content to your terminal. As with other CLI arguments, words passed to `add` or `test` can appear in shell history and process listings. Use manual file editing or stdin for preview when appropriate.

## Tiny optional dialog

```bash
local-dictate words gui
```

This explicit command opens a two-field Zenity form: **Heard** and **Replace with**. Zenity is optional and already installed on the reference machine. The CLI works without it. No dialog launches during dictation, and there is no background GUI or hotkey change.

Cancel leaves the dictionary unchanged. Changing an existing target requires a confirmation dialog. No mutation lock is held while you decide. The final update rereads the latest file, preserves other rules, and rejects an overwrite if that rule changed after confirmation was requested. Retry if this happens. Adding a rule while the dictionary is disabled leaves it disabled and prints a warning. A dialog that fails, returns malformed output, or remains open for five minutes exits without saving.

## Matching policy

- Sources match case-insensitively using Unicode case folding. The configured target's case is used exactly, so `Orange`, `orange`, and `ORANGE` all produce `Omarchy`.
- Canonically equivalent Unicode spellings match, including composed `é` and `e` followed by a combining acute accent. Matching internally uses canonical decomposition and case folding, with original-text offsets. Compatibility normalization is not used: typographic ligatures are equivalent only where Unicode case folding itself expands them. A match cannot split a case-fold expansion or a base-plus-combining-mark sequence. `Straße` and `STRASSE` collide as sources and cannot coexist with separate targets.
- Boundaries treat Unicode letters, marks, numbers, and connector punctuation (including `_`) as word characters. `oranges`, `myOrange`, `_Orange`, `orange2`, and `Orange` followed by a combining mark do not match an `Orange` rule. Adjacent Unicode letters and connector characters also block matching.
- Other punctuation delimits words. `(Orange)`, `Orange's`, and `Orange-like` become `(Omarchy)`, `Omarchy's`, and `Omarchy-like`. The same boundary requirement applies to sources containing punctuation.
- Sources are literal text. Characters such as `+`, `[`, `$`, quotes, and backslashes have no regular-expression, replacement-backreference, or shell meaning. Phrases and names may contain ordinary spaces.
- Processing goes left to right. At the same starting position, the longest valid source wins, so `New York` takes precedence over `New`. A longer candidate with an invalid end boundary does not hide a valid shorter match.
- Processing is one pass over the original input. With `A = B` and `B = C`, `A B` becomes `B C`. Replacements are not rescanned. A separate second invocation may change the text again.
- The client's existing plain-text policy runs before and after corrections: controls and whitespace become spaces, repeated spaces collapse, and outer spaces disappear. Therefore phrase sources should use single ordinary spaces. For example, recognized `New` plus a newline plus `York` can match `New York`. A source containing doubled spaces will not match sanitized dictation. Target whitespace is also subject to this policy. Unmatched characters and punctuation retain their original spelling and normalization apart from this existing whitespace/control sanitization.

There is no capitalization guessing, fuzzy replacement, or automatic correction of unknown terms. Controls, tabs, newlines, format characters, and other non-text control categories are rejected in rules. Corrections cannot introduce Enter or Tab through the delivery path.

## File, snapshots, and safety

The default dictionary is `${XDG_CONFIG_HOME:-$HOME/.config}/local-dictate/words.toml`. Unset and empty `XDG_CONFIG_HOME` both use `$HOME/.config`. With `local-dictate --config /somewhere/config.toml ...`, the dictionary is `/somewhere/words.toml`, beside the selected config path, including when the desktop config is absent. A desktop config symlink does not redirect this sibling path to its target directory. `words path` prints the absolute selection. There is no new desktop config key.

The dictionary is independent of `config.toml` and `asr.toml`:

```toml
enabled = true

[replacements]
"Orange" = "Omarchy"
"New York" = "NYC"
```

See [words.example.toml](../words.example.toml). Edit the file manually if preferred. Once a dictionary exists, its direct parent must be owned by you with no group/other permissions (normally mode 0700); the file and its adjacent lock must be private user-owned regular files (normally mode 0600), without symlinks or hard links. A missing dictionary remains a no-op beside an existing desktop config, including one in an ordinary or symlinked directory. Existing dictionaries and all mutations require the direct parent to be a real directory, not a symlink. Creating a dictionary in an ordinary directory requires making that directory private first. CLI updates create private storage when absent and do not change existing directory permissions or desktop/model settings. Unsafe existing storage is refused, not repaired automatically. Do not copy the example with broad file permissions and expect it to be accepted.

CLI mutations serialize with a two-second lock deadline, reread current rules, and atomically replace the mode-0600 file after synchronization. They preserve other rules and the enabled flag. CLI editing rewrites dictionary formatting and comments, so keep lasting notes elsewhere. Unknown settings, malformed TOML, duplicate keys, case/canonically equivalent source collisions, invalid types, and excessive sizes cause a content-free error. Rules are limited to 256 entries, 128 source characters and 512 target characters each, in a file of at most 256 KiB. Input is limited to 256 Ki characters and 1 MiB UTF-8; corrected output is limited to 1 MiB UTF-8. Expansion is checked while building output, before joining a potentially excessive result.

A new desktop owner validates rules before starting and loads its own immutable session snapshot before microphone capture. Only the dictionary path is passed in the owner's arguments, never the rule values. Edits affect the next session. WAV transcription takes the same snapshot before reading or sending audio. Invalid rules or excessive output fail before typing or copying; they are never silently skipped for recorded speech. `status`, `stop`, `cancel`, and stopping an existing session through `toggle` remain available even if the desktop config or dictionary has been edited badly. `doctor` and `words check` report dictionary problems without recording.

No transcripts or audio are stored by this feature. Normal dictation notifications contain state and content-free errors, never dictionary entries or recognized text. Existing clipboard fallback can still be retained by a clipboard manager's history; corrections do not alter that policy. Manual editors generally do not share the CLI lock. Changes observed during commit are rejected, but arbitrary same-user file replacement at the final instant cannot be made perfectly transactional.

The earlier council's 170-test approval covers the historical client/model baseline. The separate [correction-feature re-review](WORDS-REVIEW.md) approved with caveats after 221 tests passed, including a regression fix for missing dictionaries beneath symlinked config directories. Automated feature tests use temporary dictionaries, synthetic audio, local fake WebSocket servers, and mocked desktop/GUI tools. An additional native Ultra check used a bundled public WAV and temporary rules. Real Zenity interaction and real microphone-to-application behavior still need explicit user acceptance.
