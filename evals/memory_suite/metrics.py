"""Scoring for the memory suite.

Every scorer returns a small dict with a 0..1 ``score`` plus human-readable
detail, so the report can show both a number and *why*. The conversation model
is stochastic, so the harness runs several samples and averages these.
"""


def _has_any(haystack_lower: str, synonyms: list[str]) -> bool:
    return any(s.lower() in haystack_lower for s in synonyms)


# Distinctive tokens that ONLY appear in the extract-memories prompt's worked
# examples. If one shows up in a stored fact but never in the actual
# conversation, the model copied it from the prompt — classic few-shot leakage.
# (We can't blanket-forbid Cutlass/Carrack: those appear in the example AND are
# legitimate facts in some scenarios, so we check "not in conversation" instead.)
EXAMPLE_LEAK_TOKENS = ["capricorn", "theo", "microtech",  # old example (regression guard)
                       "mia", "leo", "red foxes", "reclaimer", "new babbage"]


def score_extraction(facts: list[str], scenario, conv_text: str = "") -> dict:
    """Precision/recall of stored facts against the scenario's labels.

    recall    = fraction of expected concepts that were captured
    precision = fraction of stored facts that are not forbidden (false positives)
    dedup     = 1.0 unless an at_most_one concept occupies >1 fact
    """
    lowered = [f.lower() for f in facts]

    captured, missed = [], []
    for concept in scenario.expect_facts:
        if any(_has_any(lo, concept) for lo in lowered):
            captured.append(concept[0])
        else:
            missed.append(concept[0])

    forbidden_hits = []
    for bad in scenario.forbid_facts:
        for f, lo in zip(facts, lowered):
            if bad.lower() in lo:
                forbidden_hits.append({"forbidden": bad, "fact": f})

    # Hallucination guard: an example token in a fact but not in the conversation.
    conv_lo = (conv_text or "").lower()
    for token in EXAMPLE_LEAK_TOKENS:
        if token in conv_lo:
            continue
        for f, lo in zip(facts, lowered):
            if token in lo:
                forbidden_hits.append({"forbidden": f"hallucinated:{token}", "fact": f})

    dedup_violations = []
    for concept in scenario.at_most_one:
        n = sum(1 for lo in lowered if _has_any(lo, concept))
        if n > 1:
            dedup_violations.append({"concept": concept[0], "count": n})

    n_expected = len(scenario.expect_facts)
    recall = len(captured) / n_expected if n_expected else 1.0
    if facts:
        precision = 1.0 - len(forbidden_hits) / len(facts)
    else:
        # No facts stored: perfect if none were expected, else a total miss.
        precision = 1.0 if n_expected == 0 else 0.0
    dedup = 1.0 if not dedup_violations else 0.0

    # Empty-by-design scenarios: the whole point is to store nothing.
    if n_expected == 0:
        score = 1.0 if not facts else max(0.0, 1.0 - 0.34 * len(facts))
    else:
        score = round(0.6 * recall + 0.3 * precision + 0.1 * dedup, 3)

    return {
        "score": round(score, 3),
        "recall": round(recall, 3),
        "precision": round(precision, 3),
        "captured": captured,
        "missed": missed,
        "forbidden_hits": forbidden_hits,
        "dedup_violations": dedup_violations,
        "fact_count": len(facts),
    }


def score_recall_probe(context: str, probe) -> dict:
    """Did build_memory_context surface what the probe expected (and nothing
    it forbade)?"""
    lo = (context or "").lower()
    missing = []
    for concept in probe.expect_any:
        if not _has_any(lo, concept):
            missing.append(concept[0])
    leaked = [bad for bad in probe.expect_absent if bad.lower() in lo]
    passed = not missing and not leaked
    return {
        "query": probe.query,
        "passed": passed,
        "missing": missing,
        "leaked": leaked,
        "context": context,
    }


def score_forget_probe(facts_after: list[str], probe, deleted: bool) -> dict:
    lowered = [f.lower() for f in facts_after]
    still_present = [g for g in probe.expect_gone
                     if any(g.lower() in lo for lo in lowered)]
    lost_kept = [k for k in probe.expect_kept
                 if not any(k.lower() in lo for lo in lowered)]
    passed = deleted and not still_present and not lost_kept
    return {
        "query": probe.query,
        "passed": passed,
        "deleted_something": deleted,
        "still_present": still_present,
        "wrongly_removed": lost_kept,
    }


def score_edit_probe(context: str, probe) -> dict:
    lo = (context or "").lower()
    missing = [c[0] for c in probe.expect_any if not _has_any(lo, c)]
    leaked = [bad for bad in probe.expect_absent if bad.lower() in lo]
    passed = not missing and not leaked
    return {
        "find_query": probe.find_query,
        "recall_query": probe.recall_query,
        "passed": passed,
        "missing": missing,
        "leaked": leaked,
        "context": context,
    }


_GREETING_META = ["[", "]", "<mem>", "</mem>"]


def score_greeting(text: str, summary: str) -> dict:
    """Light quality check on a returning greeting built from a session summary."""
    t = text or ""
    wc = len(t.split())
    problems = []
    if not (6 <= wc <= 50):
        problems.append(f"{wc} words")
    if "[" in t or "]" in t:
        problems.append("literal brackets")
    # A returning greeting should nod to the remembered session somehow. We can't
    # demand exact tokens (it's creative), so just flag an empty/criticaly short one.
    if wc < 6:
        problems.append("empty/too short")
    return {
        "score": 1.0 if not problems else 0.0,
        "text": t,
        "word_count": wc,
        "problems": problems,
    }


def aggregate(scenario_results: list[dict]) -> dict:
    """Roll per-scenario results into headline numbers for the report."""
    def avg(key, items):
        vals = [i[key] for i in items if key in i and i[key] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    extraction = [r["extraction"] for r in scenario_results if r.get("extraction")]
    recall_probes = [p for r in scenario_results for p in r.get("recall", [])]
    forget_probes = [p for r in scenario_results for p in r.get("forget", [])]
    edit_probes = [p for r in scenario_results for p in r.get("edits", [])]

    recall_pass = sum(1 for p in recall_probes if p["passed"])
    forget_pass = sum(1 for p in forget_probes if p["passed"])
    edit_pass = sum(1 for p in edit_probes if p["passed"])

    return {
        "extraction_score": avg("score", extraction),
        "extraction_recall": avg("recall", extraction),
        "extraction_precision": avg("precision", extraction),
        "recall_probes": f"{recall_pass}/{len(recall_probes)}" if recall_probes else "—",
        "recall_rate": round(recall_pass / len(recall_probes), 3) if recall_probes else None,
        "forget_probes": f"{forget_pass}/{len(forget_probes)}" if forget_probes else "—",
        "edit_probes": f"{edit_pass}/{len(edit_probes)}" if edit_probes else "—",
    }
