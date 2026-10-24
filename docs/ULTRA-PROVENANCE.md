# Parakeet Ultra provenance and evaluation notes

Research snapshot: 2026-10-24. The user requested an optional, config-driven upgrade from NVIDIA Parakeet TDT 0.6B v3 int8 to a compatible Moondream Parakeet Ultra int8 export. Keep the existing CPU sherpa-onnx server architecture and preserve v3 for rollback. Research findings are not an assertion that Ultra has already been downloaded, validated, or activated.

## Official model

- [Moondream release announcement](https://moondream.ai/blog/introducing-parakeet-redux-and-ultra), dated 2026-09-22.
- [Moondream Parakeet Ultra](https://huggingface.co/moondream/parakeet-ultra), release revision `73175eb7aeb0d82f1e2a6b53b3aabc10a90bcd0b`.
- [NVIDIA Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), comparison revision `541d1f99c6b0c3cd0b11a95167540bb8edefd82b`.
- Ultra is a post-trained derivative of v3. The source model card describes the same recognition architecture and tokenizer, with an added voice activity head.
- Hugging Face safetensors metadata reports 627270663 parameters for Ultra and 627057286 for v3. All 723 shared tensor names have matching shapes; six additional VAD-head tensors account for 213377 parameters. Both are accurately described as 0.6B models, but exact totals are not identical.
- At the compared revisions, tokenizer JSON files have the same ETag `a10a554cf61e38108756650018d5781ae9675c1d` and size 1159960 bytes. Configuration JSON ETags also match (`969c65410a736e4a3922f968b3481b2418aba786`). These observations support the shared tokenizer/configuration claim, not compatibility of every downstream export.

## What the scores establish

The official card reports average WER of 5.80% for Ultra versus 6.26% for v3 over seven English test sets. Lower is better. It also reports a 25-language FLEURS average of 9.55% versus 11.62%, with improvements for every listed language. One separate business-speech subset, cleaned VoxPopuli, regresses from 3.02% to 3.10%, although that group's average improves.

The current [Open ASR leaderboard listing](https://huggingface.co/datasets/hf-audio/open-asr-leaderboard) places Ultra fifth with a score of 5.32. That average includes an eighth dataset, TED-LIUM, so it should not be confused with the seven-set 5.80 figure. The model release identifies the submitted evaluation as self-reported.

These are publisher/submission results, not this project's reproduced benchmark. The official methodology evaluates Ultra with Photon on an NVIDIA GPU and v3 with NeMo in bf16. It does not establish the WER, latency, or memory use of the community CPU sherpa-onnx int8 export. Do not promise identical parameter totals, zero RAM, or that a tiny bundled-sample smoke test proves better overall accuracy.

## Export and activation requirements

The official repository provides safetensors, not an official sherpa-onnx int8 release. A community export must be separately identified, pinned to a revision, checked against published file hashes, and exercised with the installed native binary before activation. Its filenames, graph metadata, and runtime requirements may differ from the baseline export even when the recognition architecture matches.

The screenshot names `mldecode/sherpa-onnx-parakeet-ultra-int8`, whose audited revision contains no weights. The newer [mldecode/parakeet-ultra-onnx-int8](https://huggingface.co/mldecode/parakeet-ultra-onnx-int8) was investigated, but a more auditable export was selected for native validation: [leogaudin/sherpa-onnx-nemo-parakeet-ultra-int8](https://huggingface.co/leogaudin/sherpa-onnx-nemo-parakeet-ultra-int8/tree/f7bc472677ff70be95cdb0ef9df509fee6f5ec0b), pinned at `f7bc472677ff70be95cdb0ef9df509fee6f5ec0b`. It publishes a conversion script and SHA256SUMS and explicitly targets sherpa 1.13.8. All four downloaded file hashes and sizes were verified. Native CPU recognition succeeded on EN, DE, ES, and FR public samples. See [ULTRA-NATIVE-VALIDATION.md](ULTRA-NATIVE-VALIDATION.md) for measured resources, commands, and limitations. The conversion script was inspected as inert text, not executed, and no heavyweight training framework was installed. Its source download revisions are unpinned, so the exact export bytes are verified but the publisher's conversion has not been reproduced.

Both upstream models are listed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Credit Moondream and NVIDIA, link the source model and license, and identify ONNX export/int8 quantization and any further changes when redistributing. No upstream endorsement is implied. See [ATTRIBUTION.md](ATTRIBUTION.md).
