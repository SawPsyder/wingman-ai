# Local-model memory evals

Tools for understanding and tuning what the tiny local model (Qwen3.5) actually
does with our memory prompts, instead of guessing.

## Live extraction eval

`run_memory_eval.py` runs labeled conversations (`memory_extraction_cases.py`)
through the **real** extraction path — the `extract-memories` prompt, your
configured local model, and the actual parsing/dedup code — then scores the
stored facts against each case's expectations.

```bash
# Close the Wingman desktop app first (it holds the local-model ports).
python evals/run_memory_eval.py            # all cases
python evals/run_memory_eval.py ship org   # only matching case names
```

It uses your real settings (model, `n_ctx`, `run_locally`) but writes to a
throwaway temp DB, so your real persistent memory is untouched. Each case prints
the extracted facts, the summary, latency, and any failures.

It then runs a **reasoning output-budget measurement**: with reasoning on, it
reports the `<think>` and answer token counts per prompt and whether any
truncated, then suggests a `REASONING_OUTPUT_TOKENS` value. Use this to prove a
good default — if cases truncate, raise the reservation (or the user's `n_ctx`).

Add cases as you find failure modes — especially conversations that *should*
yield nothing, and ones where the assistant's statements must not leak into
facts.

## Sampling preset verification

`verify_sampling_presets.py` is a deterministic, **model-free** check that the
`SamplingPreset`s (PRECISE / BALANCED / CREATIVE) actually do what they claim:
each resolves to its documented `temperature` / `top_p` / `top_k` /
`presence_penalty`, those values reach the provider and the real completion
request, explicit args override the preset, a naked call falls back to the
Qwen3.5 default constants, and `reasoning` stays an independent param (presets
never set it).

```bash
python evals/verify_sampling_presets.py   # exit 0 = all checks passed
```

Run it after touching `SamplingPreset`, the sampling-resolution logic in
`LocalAiService.support`, or the provider request building. It needs no model,
so it's safe for CI.

Presets are the dev-friendly way to set sampling; `reasoning` is deliberately
separate because it trades latency for quality — see below.

## Debug log from real usage

Every extraction and greeting is logged (input + raw model output) to:

```text
<persistent_memory_dir>/memory_debug.jsonl
```

Turn real conversations into eval cases by inspecting that file. Disable with
`WINGMAN_MEMORY_DEBUG_LOG=0`; it rotates at ~5 MB.

## Per-task reasoning

Reasoning is a per-call option (`reasoning=True` on `LocalAiService.support()`
and the skills facade), available for capable models via the playground or a
skill opt-in. The support server is always launched with thinking *available*
so the per-call toggle works across llama.cpp builds.

**Memory extraction and condensation run with reasoning OFF.** The bundled 2B
model rambles 3000+ `<think>` tokens and truncates before answering (0 facts) —
proven by `run_memory_eval.py`'s reasoning diagnostic. Non-reasoning + the
current prompt extracts clean facts instantly. Reasoning stays available for
larger models that can think concisely; it is not forced on the 2B baseline.
