"""Labeled cases for evaluating local-model memory extraction.

Each case is a short conversation plus an ``expect`` block describing what a
*good* extraction should and should not produce. The harness in
``run_memory_eval.py`` runs each conversation through the real extraction path
(prompt + local model + parsing) and scores the stored facts against ``expect``.

Expectation keys (all optional):
    should_be_empty: bool
        The facts list must be empty (nothing durable to remember).
    min_facts / max_facts: int
        Bounds on the number of stored facts.
    forbidden_substrings: list[str]
        Case-insensitive; FAIL if any appears in any fact. Use for the
        categories the prompt forbids (current location, assistant statements,
        prices, lore, vague filler).
    required_any: list[list[str]]
        Each inner list is one required concept; PASS only if, for every
        concept, at least one fact contains one of its substrings. Phrasing is
        free, so list a few acceptable spellings.
    summary_forbidden / summary_required: list[str]
        Same idea, applied to the session summary.

Conversations need >= 4 messages (MIN_MESSAGES_FOR_EXTRACTION) or extraction
is skipped by design.
"""

CASES = [
    {
        "name": "smalltalk_yields_nothing",
        "conversation": [
            {"role": "user", "content": "hello there"},
            {"role": "assistant", "content": "Greetings, pilot. ATC here."},
            {"role": "user", "content": "just saying hi, nothing else"},
            {"role": "assistant", "content": "Acknowledged. Skies are clear."},
        ],
        "expect": {"should_be_empty": True},
    },
    {
        # Regression for the real session that produced junk facts:
        # location + assistant-lookup were stored, the durable star sign nearly lost.
        "name": "stargazing_regression",
        "conversation": [
            {"role": "user", "content": "I am looking at the stars."},
            {
                "role": "assistant",
                "content": "Roger. I can provide coordinates of notable celestial objects if you like.",
            },
            {"role": "user", "content": "yes please"},
            {
                "role": "assistant",
                "content": "Prominent locations in Stanton include Crusader, Hurston, ArcCorp, microTech and their moons.",
            },
            {"role": "user", "content": 'My star sign is "Steinbock".'},
            {
                "role": "assistant",
                "content": "Understood, that is Capricorn on our charts.",
            },
        ],
        "expect": {
            "required_any": [["capricorn", "steinbock"]],
            "forbidden_substrings": [
                "stanton",
                "coordinates",
                "assistant",
                "crusader",
                "hurston",
                "celestial",
                "currently",
                "interested in",
            ],
            "max_facts": 2,
        },
    },
    {
        "name": "ship_purchase_durable",
        "conversation": [
            {"role": "user", "content": "Just finished paying off my Drake Cutlass Black!"},
            {"role": "assistant", "content": "Congratulations, that's a solid multi-role ship."},
            {"role": "user", "content": "yeah I'm parked at Lorville right now though"},
            {"role": "assistant", "content": "Understood. Safe travels out of Lorville."},
        ],
        "expect": {
            "required_any": [["cutlass"]],
            "forbidden_substrings": ["lorville", "parked", "assistant"],
            "min_facts": 1,
        },
    },
    {
        "name": "org_membership_durable",
        "conversation": [
            {"role": "user", "content": "I just joined the Aegis Dynamics Reapers org with my friend Marco."},
            {"role": "assistant", "content": "Welcome to the Reapers. Marco will show you the ropes."},
            {"role": "user", "content": "we're grinding toward a Carrack eventually"},
            {"role": "assistant", "content": "A fine goal. The Carrack is a capable explorer."},
        ],
        "expect": {
            "required_any": [["reapers"], ["marco"]],
            "forbidden_substrings": ["assistant", "welcome"],
            "min_facts": 2,
        },
    },
    {
        "name": "pure_location_not_durable",
        "conversation": [
            {"role": "user", "content": "Heading from Daymar to Yela now."},
            {"role": "assistant", "content": "Copy. Watch for pirates near the Yela belt."},
            {"role": "user", "content": "thanks, almost there"},
            {"role": "assistant", "content": "Acknowledged. Good luck out there."},
        ],
        "expect": {"should_be_empty": True},
    },
    {
        "name": "multiple_durable_facts",
        "conversation": [
            {"role": "user", "content": "Call me Jess. I fly a Constellation Phoenix and stream on Twitch."},
            {"role": "assistant", "content": "Nice to meet you, Jess. The Phoenix is a luxurious bird."},
            {"role": "user", "content": "I hate bounty hunting though, I'm all about cargo hauling."},
            {"role": "assistant", "content": "Cargo it is. I'll keep the trade routes handy."},
        ],
        "expect": {
            "required_any": [["jess"], ["constellation", "phoenix"]],
            "forbidden_substrings": ["assistant", "trade route", "luxurious"],
            "min_facts": 2,
        },
    },
]
