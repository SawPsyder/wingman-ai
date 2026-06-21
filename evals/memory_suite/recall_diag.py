"""Recall-threshold diagnostic — isolates the embedding/similarity question
from extraction noise.

For each scenario it extracts ONCE, then for every recall probe computes the
cosine similarity between the query and each stored fact. This shows the exact
similarity the right fact achieves, so we can pick MEMORY_MIN_SIMILARITY from
data instead of guessing. (The full-suite sweep re-extracts per threshold, which
mixes in the model's stochasticity; this holds the stored facts fixed.)

  python -m evals.memory_suite.recall_diag --attach
"""

import argparse
import sys
from os import path

REPO_ROOT = path.dirname(path.dirname(path.dirname(path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import os  # noqa: E402

os.environ.setdefault("WINGMAN_MEMORY_DEBUG_LOG", "0")

import services.persistent_memory as pm_mod  # noqa: E402
from evals.memory_suite.harness import ModelHost, _extract_once, _temp_memory_dir  # noqa: E402
from evals.memory_suite.profiles import DEFAULT  # noqa: E402
from evals.memory_suite.scenarios import get_scenarios  # noqa: E402
from services.persistent_memory import PersistentMemoryService, _cosine_similarity  # noqa: E402

THRESHOLDS = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--attach", action="store_true")
    ap.add_argument("--support-port", type=int, default=49172)
    ap.add_argument("--embed-port", type=int, default=49173)
    args = ap.parse_args()

    host = ModelHost(attach=args.attach,
                     support_port=args.support_port, embed_port=args.embed_port)
    if not host.ensure_loaded():
        print("ERROR: models not reachable.")
        return 1
    local_ai = host.local_ai

    # threshold -> [pass, total] over all probes, with extraction held fixed
    tally = {t: [0, 0] for t in THRESHOLDS}
    try:
        for scenario in get_scenarios():
            if not scenario.recall:
                continue
            with _temp_memory_dir():
                svc = PersistentMemoryService(wingman_name="__diag__", local_ai_service=local_ai)
                svc.initialize()
                try:
                    _extract_once(svc, local_ai, scenario, DEFAULT)
                    facts = [e.content for e in svc.get_all(entry_type="fact")]
                    fact_embeds = local_ai.embed(facts) if facts else []
                    print(f"\n### {scenario.title}")
                    print(f"    stored: {facts}")
                    for probe in scenario.recall:
                        if not probe.expect_any:
                            continue  # absence-only probes don't have a target fact
                        q_emb = local_ai.embed([probe.query])[0]
                        # best similarity to any fact that satisfies the concept
                        best_match, best_sim = None, -1.0
                        for fact, emb in zip(facts, fact_embeds):
                            sim = _cosine_similarity(q_emb, emb)
                            if sim > best_sim:
                                best_sim, best_match = sim, fact
                        # does the best-matching fact actually contain the concept?
                        target_lo = (best_match or "").lower()
                        concept = probe.expect_any[0]
                        hit = any(s.lower() in target_lo for s in concept)
                        for t in THRESHOLDS:
                            tally[t][1] += 1
                            if best_sim >= t and hit:
                                tally[t][0] += 1
                        print(f"    {best_sim:.3f}  '{probe.query}'  ->  "
                              f"'{best_match}'  {'✓' if hit else '✗concept'}")
                finally:
                    svc.close()
    finally:
        host.shutdown()

    print(f"\n{'═' * 60}\nPROBE PASS RATE BY THRESHOLD (extraction held fixed)")
    for t in THRESHOLDS:
        p, n = tally[t]
        bar = "█" * round(20 * p / n) if n else ""
        print(f"  {t:.2f}  {p:2}/{n}  {bar}")
    cur = pm_mod.MEMORY_MIN_SIMILARITY
    print(f"\n  current MEMORY_MIN_SIMILARITY = {cur}")
    best = max(THRESHOLDS, key=lambda t: (tally[t][0], -t))
    print(f"  best probe coverage at threshold = {best}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
