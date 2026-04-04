#!/usr/bin/env python3
"""Test prompts via the Local AI Lab playground endpoint.

Usage:
    python scripts/test_tts_praise.py [--prompt tts-test-praise] [--iterations 3] [--host http://localhost:49111]

Runs the given prompt through all sampling presets and prints results + analysis.
Presets based on Qwen3.5 recommended sampling parameters.
Designed for quick prompt/param tuning from the command line.
"""

from __future__ import annotations

import argparse
import json
import statistics
import urllib.request

# Presets based on Qwen3.5-2B HuggingFace recommendations:
# Non-thinking text: temp=1.0, top_p=1.0, top_k=20, presence_penalty=2.0
# Thinking text:     temp=1.0, top_p=0.95, top_k=20, presence_penalty=1.5
# Precise coding:    temp=0.6, top_p=0.95, top_k=20, presence_penalty=0.0
SAMPLING_PRESETS = [
    #  name           temp  top_p  top_k  presence_penalty
    ("Precise",       0.6,  0.95,  20,    0.0),
    ("Balanced",      1.0,  0.95,  20,    1.5),
    ("Creative",      1.0,  1.0,   20,    2.0),
]


def call_playground(host: str, system_prompt: str, user_message: str,
                    iterations: int, temperature: float, top_p: float,
                    top_k: int = 20, presence_penalty: float = 2.0):
    payload = {
        "system_message": system_prompt,
        "user_message": user_message,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "presence_penalty": presence_penalty,
        "iterations": iterations,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/settings/local-ai/playground/chat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())


def load_prompt(host: str, name: str) -> str | None:
    resp = urllib.request.urlopen(f"{host}/settings/local-ai/playground/prompts")
    prompts = json.loads(resp.read())
    return next((p["content"] for p in prompts if p["name"] == name), None)


def analyze_preset(responses: list[dict]) -> dict:
    """Compute stats for a set of responses."""
    texts = [r["text"].strip() for r in responses]
    word_counts = [len(t.split()) for t in texts]
    token_counts = [r["completion_tokens"] for r in responses]
    times = [r["time_ms"] for r in responses]

    return {
        "texts": texts,
        "word_counts": word_counts,
        "avg_words": statistics.mean(word_counts),
        "avg_tokens": statistics.mean(token_counts),
        "avg_ms": statistics.mean(times),
        "word_stddev": statistics.stdev(word_counts) if len(word_counts) > 1 else 0,
        "unique_ratio": len(set(texts)) / len(texts) if texts else 0,
    }


def print_results(name: str, temp: float, top_p: float, top_k: int,
                  presence_penalty: float, stats: dict):
    print(f"\n{'─' * 90}")
    print(f"  {name}  (temp={temp}, top_p={top_p}, top_k={top_k}, presence_penalty={presence_penalty})")
    print(f"{'─' * 90}")

    for i, text in enumerate(stats["texts"], 1):
        wc = stats["word_counts"][i - 1]
        print(f"  {i:2d}. [{wc:2d}w] {text}")

    print(f"\n  Stats: avg {stats['avg_words']:.1f} words (+-{stats['word_stddev']:.1f}), "
          f"{stats['avg_tokens']:.0f} tokens, {stats['avg_ms']:.0f}ms/response, "
          f"{stats['unique_ratio']:.0%} unique")


def print_analysis(all_results: dict):
    print(f"\n{'=' * 90}")
    print("  ANALYSIS")
    print(f"{'=' * 90}")

    print(f"\n  {'Preset':<14} {'Avg Words':>10} {'StdDev':>8} {'Avg Tokens':>11} {'Avg ms':>8} {'Unique':>8}")
    print(f"  {'─' * 14} {'─' * 10} {'─' * 8} {'─' * 11} {'─' * 8} {'─' * 8}")

    for name, stats in all_results.items():
        print(f"  {name:<14} {stats['avg_words']:>10.1f} {stats['word_stddev']:>8.1f} "
              f"{stats['avg_tokens']:>11.0f} {stats['avg_ms']:>8.0f} {stats['unique_ratio']:>7.0%}")

    best = min(all_results.items(),
               key=lambda x: abs(x[1]["avg_words"] - 15) + (1 - x[1]["unique_ratio"]) * 20)
    print(f"\n  Recommendation: '{best[0]}' -- best balance of length (~15w target) and variety")


def main():
    parser = argparse.ArgumentParser(description="Test prompts via Local AI Lab playground")
    parser.add_argument("--prompt", default="tts-test-praise",
                        help="Prompt name from prompts/ dir (default: tts-test-praise)")
    parser.add_argument("--user-message", default="Generate a sentence.",
                        help="User message to send (default: 'Generate a sentence.')")
    parser.add_argument("--iterations", type=int, default=3,
                        help="Iterations per preset (default: 3)")
    parser.add_argument("--host", default="http://localhost:49111",
                        help="Core API base URL")
    args = parser.parse_args()

    system_prompt = load_prompt(args.host, args.prompt)
    if not system_prompt:
        print(f"ERROR: prompt '{args.prompt}' not found. Available prompts:")
        resp = urllib.request.urlopen(f"{args.host}/settings/local-ai/playground/prompts")
        for p in json.loads(resp.read()):
            print(f"  - {p['name']}")
        return

    print(f"Prompt: {args.prompt}")
    print(f"System: {system_prompt.strip()[:120]}...")
    print(f"User:   {args.user_message}")
    print(f"Iterations per preset: {args.iterations}")
    print(f"{'=' * 90}")

    all_results = {}

    for name, temp, top_p, top_k, pp in SAMPLING_PRESETS:
        try:
            result = call_playground(args.host, system_prompt, args.user_message,
                                     args.iterations, temp, top_p, top_k, pp)
        except Exception as e:
            print(f"\n  {name}: ERROR -- {e}")
            continue

        if not result.get("success"):
            print(f"\n  {name}: FAILED -- {result.get('error', 'unknown')}")
            continue

        stats = analyze_preset(result["responses"])
        all_results[name] = stats
        print_results(name, temp, top_p, top_k, pp, stats)

    if all_results:
        print_analysis(all_results)

    print(f"\n{'=' * 90}")
    print("Done.")


if __name__ == "__main__":
    main()
