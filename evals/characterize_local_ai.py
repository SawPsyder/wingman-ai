"""Characterize the bundled local support model across the tasks Wingman uses it
for. Dumps the EXACT system prompt + user input + raw output per case, scores with
light heuristics, and can sweep presets or A/B prompt variants.

Goal: understand what the 2B model does well/badly with our prompts and presets,
so we can optimize them empirically instead of guessing.

Usage (close the desktop app first — it holds the model ports):
  python evals/characterize_local_ai.py                # all tasks, production presets
  python evals/characterize_local_ai.py extract        # only matching task(s)
  python evals/characterize_local_ai.py --sweep extract  # try every preset on a task
  python evals/characterize_local_ai.py --quiet        # scores only, no full I/O dump
"""

import argparse
import json
import sys
from os import path

REPO_ROOT = path.dirname(path.dirname(path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import os  # noqa: E402

os.environ.setdefault("WINGMAN_MEMORY_DEBUG_LOG", "0")

from evals.run_memory_eval import _build_local_ai  # noqa: E402
from services.file import get_prompt  # noqa: E402
from services.skill_local_ai import SamplingPreset  # noqa: E402
from services.token_utils import count_tokens  # noqa: E402

# ── helpers ──────────────────────────────────────────────────────────


def conv(messages):
    """Join a [(role, text), ...] conversation the way extraction does."""
    return "\n".join(f"{r}: {t}" for r, t in messages)


def parse_json(text):
    if not text:
        return None
    import re

    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                return None
    return None


def word_count(text):
    return len((text or "").split())


# ── scorers (return list of problem strings; empty = pass) ───────────


def score_extraction(out, expect):
    problems = []
    data = parse_json(out)
    if data is None:
        return ["invalid JSON"]
    facts = data.get("facts", [])
    if not isinstance(facts, list):
        return ["facts is not a list"]
    lowered = [str(f).lower() for f in facts]
    if expect.get("empty") and facts:
        problems.append(f"expected empty, got {facts}")
    for bad in expect.get("forbidden", []):
        if any(bad in lo for lo in lowered):
            problems.append(f"forbidden '{bad}' in facts")
    for concept in expect.get("required_any", []):
        if not any(any(s in lo for s in concept) for lo in lowered):
            problems.append(f"missing {concept}")
    if any("unknown" in lo or "not mentioned" in lo or "none" == lo for lo in lowered):
        problems.append("placeholder fact")
    return problems


def score_greeting(out, expect):
    problems = []
    text = out or ""
    wc = word_count(text)
    lo, hi = expect.get("words", (7, 45))
    if not (lo <= wc <= hi):
        problems.append(f"{wc} words (want {lo}-{hi})")
    if "?" in text and expect.get("no_question"):
        problems.append("asks a question")
    for bad in expect.get("forbidden", []):
        if bad.lower() in text.lower():
            problems.append(f"leaked '{bad}' (not in memory)")
    if expect.get("needs_mem") and "<mem>" not in text:
        problems.append("no <mem> tags")
    if "[" in text or "]" in text:
        problems.append("literal placeholder brackets in output")
    if text.count("<mem>") != text.count("</mem>"):
        problems.append("unbalanced <mem> tags")
    if expect.get("no_name") and expect["no_name"].lower() in text.lower():
        problems.append("introduces own name")
    return problems


_META_PHRASES = [
    "no additional fact", "no further", "conversation concludes",
    "summary itself", "no more fact", "as requested", "i have listed",
    "in summary", "to summarize", "note that there",
]


def score_summary(out, expect):
    problems = []
    text = (out or "").lower()
    if word_count(out) < expect.get("min_words", 8):
        problems.append("too short / empty")
    for need in expect.get("must_mention", []):
        if not any(s in text for s in need):
            problems.append(f"missing {need}")
    for bad in expect.get("forbidden", []):
        if bad.lower() in text:
            problems.append(f"forbidden '{bad}'")
    if expect.get("no_meta"):
        for phrase in _META_PHRASES:
            if phrase in text:
                problems.append(f"meta-commentary '{phrase}'")
                break
    return problems


# ── tasks ────────────────────────────────────────────────────────────


def build_tasks():
    extract_sys = get_prompt("extract-memories")
    condense_sys = get_prompt("condense-conversation")
    greet_ret_sys = get_prompt("greeting-returning")
    greet_def_sys = get_prompt("greeting-default")
    tool_sys = get_prompt("support-tool-response")

    def condense_user(c):
        return (
            "CONVERSATION TO SUMMARIZE:\n" + c + "\n\n---\n"
            "Now list every fact from the conversation above as bullet points.\n"
            "Start from the FIRST message, end at the LAST. Include all names, "
            "preferences, and creative content. Never include secrets, API keys, "
            "credentials, passwords, or tokens:"
        )

    def tool_user(j):
        return (
            "DATA TO SUMMARIZE:\n" + j + "\n\n---\n"
            "Summarize the above data. Preserve all key facts, numbers, names, "
            "IDs, and status values:"
        )

    return [
        {
            "task": "extract-memories",
            "system": extract_sys,
            "preset": SamplingPreset.PRECISE,
            "scorer": score_extraction,
            "cases": [
                ("durable_ship_starsign",
                 conv([("user", "Just paid off my Drake Cutlass Black. I'm a Capricorn."),
                       ("assistant", "Nice! Coordinates for microTech are below."),
                       ("user", "thanks, parked at Lorville for now")]),
                 {"required_any": [["cutlass"], ["capricorn"]], "forbidden": ["lorville", "microtech", "parked"]}),
                ("smalltalk_empty",
                 conv([("user", "hey there"), ("assistant", "Greetings, pilot."),
                       ("user", "just cruising, nothing else")]),
                 {"empty": True}),
                ("org_friend_goal",
                 conv([("user", "Joined the Aegis Reapers org with my friend Marco."),
                       ("assistant", "Welcome aboard."),
                       ("user", "saving up for a Carrack eventually")]),
                 {"required_any": [["reapers"], ["marco"], ["carrack"]], "forbidden": ["welcome"]}),
                ("german_mixed",
                 conv([("user", "Ich heiße Lukas und fliege eine Avenger Titan."),
                       ("assistant", "Schön dich kennenzulernen, Lukas."),
                       ("user", "ich bin gerade bei Hurston unterwegs")]),
                 {"required_any": [["lukas"], ["avenger", "titan"]], "forbidden": ["hurston"]}),
                ("self_correction",
                 conv([("user", "I main a Gladius. Actually no, I sold it, I fly a Hornet now."),
                       ("assistant", "Got it, the Hornet."),
                       ("user", "yeah the F7C")]),
                 {"required_any": [["hornet"]], "forbidden": ["gladius"]}),
                ("long_multi_fact",
                 conv([("user", "Name's Dana, 34, from Berlin. I run the org Black Sails."),
                       ("assistant", "Hello Dana."),
                       ("user", "I hate mining but love exploration. Goal is to map every jump point."),
                       ("assistant", "Ambitious!"),
                       ("user", "I fly a Carrack for that, with my friend Tomas")]),
                 {"required_any": [["dana"], ["black sails"], ["carrack"], ["exploration", "explore", "jump point"]]}),
            ],
        },
        {
            "task": "condense-conversation",
            "system": condense_sys,
            "preset": SamplingPreset.BALANCED,
            "scorer": score_summary,
            "user_wrap": condense_user,
            "cases": [
                ("trade_run",
                 conv([("user", "Best ship for solo bounty hunting?"),
                       ("assistant", "The Vanguard Warden — heavy firepower, strong shields."),
                       ("user", "I keep dying to Hammerheads in my Cutlass."),
                       ("assistant", "Hit-and-run: hit one shield face, boost away, aim engines."),
                       ("user", "Org bounty night is Saturday, I'll save them for then.")]),
                 {"must_mention": [["cutlass"], ["hammerhead"]], "min_words": 15, "no_meta": True}),
                ("short_chat",
                 conv([("user", "What's the time to Yela?"),
                       ("assistant", "About 6 minutes at quantum speed."),
                       ("user", "thanks")]),
                 {"min_words": 8, "no_meta": True}),
            ],
        },
        {
            "task": "greeting-returning",
            "system": None,  # formatted per case
            "preset": SamplingPreset.CREATIVE,
            "scorer": score_greeting,
            "format": greet_ret_sys,
            "cases": [
                ("ship_memory",
                 {"name": "ATC", "backstory": "A gruff but kind air traffic controller.",
                  "session_summary": "The user flies a Reclaimer and was hauling salvage near Daymar."},
                 {"words": (8, 45), "no_question": False, "needs_mem": True,
                  "forbidden": ["Constellation Andromeda", "Hurston"]}),
                ("org_memory",
                 {"name": "Nova", "backstory": "An upbeat ship AI companion.",
                  "session_summary": "The user joined the Black Sails org and is saving for a Carrack."},
                 {"words": (8, 45), "needs_mem": True,
                  "forbidden": ["Constellation Andromeda", "Hurston"]}),
                ("thin_memory",
                 {"name": "ATC", "backstory": "A gruff but kind air traffic controller.",
                  "session_summary": "The user said hello and asked about the weather."},
                 {"words": (8, 45), "forbidden": ["Constellation Andromeda", "Hurston"]}),
            ],
        },
        {
            "task": "greeting-default",
            "system": None,
            "preset": SamplingPreset.BALANCED,
            "scorer": score_greeting,
            "format": greet_def_sys,
            "cases": [
                ("atc",
                 {"name": "ATC", "backstory": "A gruff but kind air traffic controller on a busy station."},
                 {"words": (7, 40), "no_question": True, "no_name": "ATC"}),
                ("companion",
                 {"name": "Nova", "backstory": "An upbeat, witty ship AI companion who loves exploration."},
                 {"words": (7, 40), "no_question": True, "no_name": "Nova"}),
            ],
        },
        {
            "task": "support-tool-response",
            "system": tool_sys,
            "preset": SamplingPreset.PRECISE,
            "scorer": score_summary,
            "user_wrap": tool_user,
            "cases": [
                ("ship_json",
                 json.dumps({"ship": "Constellation Andromeda", "manufacturer": "RSI",
                             "crew": {"min": 1, "max": 4}, "cargo_scu": 96,
                             "price_auec": 3250000, "status": "Flight Ready"}),
                 {"must_mention": [["constellation"], ["96"], ["3,250,000", "3250000"]], "min_words": 10}),
            ],
        },
    ]


# ── runner ───────────────────────────────────────────────────────────


def run_task(local_ai, task, preset, quiet, samples):
    """Run every case `samples` times; report a pass RATE (the model is
    stochastic, so a single sample is noise — especially at high temperature)."""
    sys_template = task.get("format")
    scorer = task["scorer"]
    wrap = task.get("user_wrap", lambda x: x)
    case_rates = []  # (passes, samples) per case
    if not quiet and task.get("system"):
        print(f"\n{'─' * 70}\nSYSTEM PROMPT [{task['task']}]:\n{task['system']}\n{'─' * 70}")
    for case in task["cases"]:
        name, payload, expect = case
        if sys_template is not None:
            system_prompt = sys_template.format(**payload)
            user_input = "Generate your greeting."
        else:
            system_prompt = task["system"]
            user_input = wrap(payload)
        passes = 0
        outputs = []
        for _ in range(samples):
            res = local_ai.support(text=user_input, system_prompt=system_prompt, preset=preset)
            out = res.text if res else None
            problems = scorer(out, expect)
            passes += not problems
            outputs.append((out, problems))
        case_rates.append((passes, samples))
        if not quiet:
            if sys_template is not None:
                print(f"\nSYSTEM PROMPT [{task['task']}/{name}]:\n{system_prompt}")
            print(f"\n  CASE: {name}  preset={preset.name}  pass={passes}/{samples}")
            print(f"  USER INPUT: {user_input}")
            for i, (out, problems) in enumerate(outputs):
                tag = "PASS" if not problems else "FAIL: " + "; ".join(problems)
                print(f"  [{i + 1}] {tag}\n      {out or '<none>'}")
        else:
            print(f"  {task['task']}/{name}: {passes}/{samples}"
                  + ("" if passes == samples
                     else "  last_fail=(" + "; ".join(outputs[-1][1]) + ")"))
    return case_rates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("filters", nargs="*", help="only run tasks whose name matches")
    ap.add_argument("--sweep", metavar="TASK", help="run one task across all presets")
    ap.add_argument("--quiet", action="store_true", help="scores only, no I/O dump")
    ap.add_argument("--samples", type=int, default=1,
                    help="runs per case (use >1 for stochastic tasks like greetings)")
    args = ap.parse_args()

    local_ai, provider, settings = _build_local_ai()
    if not (provider.load_support_model() and provider.load_embed_model()):
        print("ERROR: could not load local models (close the desktop app?).")
        return 1

    tasks = build_tasks()
    try:
        if args.sweep:
            task = next((t for t in tasks if args.sweep in t["task"]), None)
            if not task:
                print(f"no task matches '{args.sweep}'")
                return 1
            print(f"\n### PRESET SWEEP: {task['task']}  (samples={args.samples})")
            for preset in SamplingPreset:
                rates = run_task(local_ai, task, preset, args.quiet, args.samples)
                p = sum(x for x, _ in rates)
                n = sum(y for _, y in rates)
                print(f"  >> {preset.name}: {p}/{n} samples passed")
            return 0

        selected = [t for t in tasks
                    if not args.filters or any(f in t["task"] for f in args.filters)]
        summary = []
        for task in selected:
            rates = run_task(local_ai, task, task["preset"], args.quiet, args.samples)
            p = sum(x for x, _ in rates)
            n = sum(y for _, y in rates)
            summary.append((task["task"], task["preset"].name, p, n))
        print(f"\n{'═' * 70}\nSUMMARY  (samples={args.samples} per case)")
        for name, preset, passed, total in summary:
            pct = f"{100 * passed // total}%" if total else "—"
            print(f"  {name:24} {preset:9} {passed}/{total}  {pct}")
        total_p = sum(p for _, _, p, _ in summary)
        total_t = sum(t for _, _, _, t in summary)
        print(f"  {'TOTAL':24} {'':9} {total_p}/{total_t}")
    finally:
        try:
            provider.unload_models()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
