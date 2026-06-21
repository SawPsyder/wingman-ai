"""End-to-end test harness for the Persistent Memory feature.

Unlike ``characterize_local_ai.py`` (which probes the support model on isolated
prompts), this suite drives the FULL persistent-memory lifecycle against real
models and a throwaway database:

    realistic conversation
        -> extract_memories  (support model + extract-memories prompt)
        -> stored facts + session summary  (embed model + dedup)
        -> recall              (embed model + similarity search)
        -> edit / forget       (update + forget_by_query)
        -> returning greeting  (support model + greeting-returning prompt)

It measures whether the model AND our defaults (n_ctx, sampling, prompts,
similarity thresholds) are good — and can sweep those knobs to find better
values. See ``run.py`` for the CLI and ``scenarios.py`` for the dataset.
"""
