# Local support model (Qwen3.5-2B) — characterization findings

Measured with `characterize_local_ai.py` (8 samples/case, n_ctx=4096, Metal).
After tuning, the tasks Wingman uses the support model for score:

| Task | Preset | Pass rate |
|------|--------|-----------|
| extract-memories | PRECISE | ~97% |
| condense-conversation | BALANCED | 100% |
| support-tool-response | PRECISE | 100% |
| greeting-default | BALANCED | ~93% |
| greeting-returning | CREATIVE | ~91% |

Start point before tuning was ~71%. Re-run any time with
`python evals/characterize_local_ai.py --samples 8` (desktop app closed).

## The big levers (in order of impact)

1. **Temperature is the #1 reliability knob for structured tasks.** For
   extraction (parse → JSON), reliability vs temperature was: 0.6 → 67%, 0.3 →
   92%, 0.1 → 98%, 0.0 → 100%. The "hard cases" (dropping a friend, keeping a
   sold ship) were **sampling variance, not a capability ceiling** — at low temp
   the 2B gets them right. `SamplingPreset.PRECISE` is now temp **0.1** (was
   0.6). Use PRECISE for any parse/transform/JSON task.

2. **Reasoning OFF for the 2B.** With thinking on, the model rambles 3000+
   `<think>` tokens and truncates before answering (0 facts). Non-reasoning is
   faster *and* correct. Reasoning stays a per-call option for capable models.

3. **The 2B is stochastic** — at high temperature (greetings, temp 1.0) a single
   run swings ±15-20 points. Always measure a *rate* over several samples, never
   one run.

## Prompt patterns that work on this model

- **Concrete worked examples beat rules.** A filled input→output example teaches
  the transformation far better than prose instructions.
- **Never put copyable placeholders in examples.** `<mem>[detail]</mem>` made the
  model emit literal `[Black Sails]`; a concrete fake example fixed it. Same root
  cause as the original "Constellation Andromeda" greeting leak.
- **Lead greetings with hard constraints + one short example.** "ONE sentence,
  10-20 words, no questions, no name" up front took greeting-default 20% → 93%.
- **Resolve rule tension explicitly.** "List every fact" re-triggered placeholder
  enumeration and location grabs; carving out "but never placeholders / never
  locations, in any language" reconciled it.
- The model handles **non-English input** well (German → correct English facts)
  and correctly drops retracted/sold items at low temperature.

## Residual ceiling (~5-10%, not worth chasing)

Self-correction edge cases, occasional `<mem>` tag omission on greetings, and a
rare stray question. These are 2B stochasticity at the high-90s; further prompt
work just chases noise. A bigger/remote support model would lift these — and the
budget/reasoning/remote-context infrastructure is ready for that.
