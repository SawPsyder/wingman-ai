# Persistent Memory — end-to-end findings (2026-06-21)

Measured with `evals/memory_suite` against the bundled Qwen3.5-2B support model +
nomic-embed-text-v1.5, n_ctx 4096, on six realistic conversations (long Star
Citizen sessions, another game, a desktop-assistant session, a German session,
and a no-durable-facts small-talk session).

The prompt-level evals (`characterize_local_ai.py`) reported extraction ~97% —
but they used 3-message toy conversations. On **realistic, longer sessions the
full pipeline scored far lower**, and the gap was invisible until we tested
extract → store → recall as one system.

## What was broken (baseline)

| Stage | Baseline | Problem |
|-------|----------|---------|
| Extraction (long SC session) | **42% recall** | Model stored ~3 of 8 facts; dropped org, second ship, friend, likes/dislikes even though they were in the conversation (and in the summary). |
| Extraction precision | 80–83% | **Hallucinated "Star sign is Capricorn"** for a user who never mentioned one — copied verbatim from the prompt's worked example (few-shot leakage, same class of bug as the old "Constellation Andromeda" greeting). |
| Recall | **10/18 probes** | Stored facts didn't resurface. Two causes, now separated. |
| Greeting | leaked | Returning greeting wrapped a *location* ("Daymar") in `<mem>` tags, because the session summary itself contained locations. |

## The two real levers

### 1. Extraction completeness (dominant)
The worked example in `extract-memories.md` packed every fact into a **single
user message**, teaching the 2B to extract from one line. Real sessions spread
facts across many turns. Rewriting the example as a **spread-out multi-turn
conversation** + adding an explicit "scan EVERY message, a session has 5–8 facts"
directive took the long-session extraction recall **42% → 65%**, and dropping the
star-sign line from the example removed the hallucination (**precision → 100%**).

### 2. Recall similarity threshold
`MEMORY_MIN_SIMILARITY` was 0.5. The embed model scores a *relevant* fact only
0.40–0.49 against a natural follow-up ("Who do I play with?" vs "Friend is named
Mara"). Because `build_memory_context` returns a token-capped **set** of facts
(not just the top-1), loosening the gate to **0.4** lands the right fact in the
batch. Sweep: probes passing were 13 / **15** / 10 / 5 / 1 at thresholds
0.3 / 0.4 / 0.5 / 0.6 / 0.7. Changed the default to **0.4**.

A separate diagnostic (`recall_diag.py`, extraction held fixed) showed **top-1
embedding accuracy is only ~41%** — for many queries the nearest neighbour is the
wrong fact (e.g. "What's my main ship?" ranks the goal fact above "Owns a
Vanguard Warden"; German queries match org over ship). The set-based recall path
masks this, but it's the ceiling a better embed model would lift.

## Result

Both changes together, across the whole suite (samples=3):

| Metric | Before | After |
|--------|--------|-------|
| Extraction score | 72% | **92%** |
| Extraction recall | 62% | **87%** |
| Extraction precision | 83% | **99%** |
| Recall probes passing | 10/18 | **17/18** |

No regressions: the small-talk session still stores nothing, German hits 100%,
and the new worked example does not leak into other scenarios (the harness guards
against that with `EXAMPLE_LEAK_TOKENS`).

## Changed defaults

- `services/persistent_memory.py`: `MEMORY_MIN_SIMILARITY` 0.5 → **0.4**.
- `prompts/extract-memories.md`: spread-out multi-turn worked example + scan
  directive; removed the star-sign line that caused the hallucination.

## Not changed (and why)

- **Extraction temperature** stays 0.1. The temperature sweep was inconclusive at
  one sample (recall bounced 63–88% on run-to-run noise); the completeness win
  came from the prompt, not sampling, and 0.1 remains best for format reliability
  and self-correction (see `../FINDINGS.md`).
- **n_ctx** stays at the user's setting. The 2B baseline isn't context-limited on
  these sessions; larger windows are for bigger remote models.

## Open items (for a future model or session)

- Top-1 embedding accuracy (~41%) — a stronger embed model would help recall most.
- Cross-session staleness: a sold ship stated in session 2 doesn't retract the
  "owns it" fact from session 1 (dedup is similarity-based, not contradiction-
  aware). Not yet covered by a scenario.
- The long SC session still tops out around 65% extraction recall — the hardest
  case; a larger support model is the lever.
