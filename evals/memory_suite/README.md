# Persistent Memory test suite

A durable, end-to-end harness for Wingman's most important v3 feature. Use it to
answer two questions on demand — when a new model ships, when a user reports a
memory bug, or when tuning defaults:

1. **Is the model good enough** at remembering across realistic conversations?
2. **Are our defaults good** (context window, sampling, prompts, similarity
   thresholds) — or is there a better value?

Unlike `../characterize_local_ai.py` (which probes the support model on isolated
prompts), this suite drives the *whole* pipeline against real models and a
throwaway database:

```
realistic conversation
   -> extract_memories   (support model + extract-memories prompt)
   -> stored facts        (embed model + dedup)
   -> recall              (embed model + similarity search)   <-- the part prompt-evals never tested
   -> edit / forget       (update + forget_by_query)
   -> returning greeting  (support model + greeting-returning prompt)
```

## Running

Two ways to reach the models:

```bash
# ATTACH (recommended): run against the ALREADY-RUNNING desktop app.
# Zero setup. Skips n_ctx/model-swap profiles (we don't own that server).
python -m evals.memory_suite.run --attach

# MANAGED: spawn our own llama-servers (close the desktop app first).
# Needed only for n_ctx / model-swap sweeps.
python -m evals.memory_suite.run
```

Common invocations:

```bash
python -m evals.memory_suite.run --attach --samples 3        # stable averages
python -m evals.memory_suite.run --attach --scenario sc_long  # one scenario, verbose
python -m evals.memory_suite.run --attach --category star_citizen
python -m evals.memory_suite.run --attach --profiles default,temp0,loose_recall
python -m evals.memory_suite.run --attach --sweep min_similarity   # find a better threshold
python -m evals.memory_suite.recall_diag --attach            # isolated recall-threshold curve
```

Output lands in `results/report.html` (open it — the visual scorecard) and
`results/results.json`. The same scorecard is available live in the desktop app
under **Settings → Local AI → Playground → Memory**.

## Anatomy

| File | What it holds |
|------|---------------|
| `scenarios.py` | The realistic conversations + labels (expected facts, forbidden facts, recall/forget/edit probes). **Add new scenarios here.** |
| `metrics.py`   | Scoring: extraction precision/recall, hallucination guard, recall@probe, dedup, forget, edit, greeting. |
| `profiles.py`  | Config profiles ("different Wingmen") + sweep axes (temp, n_ctx, min_similarity). |
| `harness.py`   | The engine — runs a scenario end-to-end under a profile. `ModelHost` handles attach vs managed. |
| `report.py`    | HTML / JSON / terminal reporting. |
| `run.py`       | CLI. |
| `recall_diag.py` | Isolated recall-threshold diagnostic (extraction held fixed). |

## Adding a scenario

Append a `Scenario` to `scenarios.py`. Spread the durable facts across several
turns and pad with transient chatter (locations, prices) so the suite measures
precision too. Label `expect_facts` (concepts that should be stored),
`forbid_facts` (things that must NOT be), and `recall` probes (follow-up
questions a user would actually ask). That's it — every profile and sweep picks
it up automatically.
