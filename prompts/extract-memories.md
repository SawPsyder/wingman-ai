Extract facts and a summary from the conversation below.

SUMMARY_HERE: 2-4 sentences about what the user did, key events, and outcome.

FACT_1, FACT_2, ...: Up to 5 durable personal facts about the user -- things still true next week. Good facts include: age, likes and dislikes, equipment they own, orgs or clans they belong to, hardware they use, friends they mentioned by name, goals they are working toward, character traits or personality.

Do NOT include:

- Current location or destination
- Session events (credits earned, items found)
- What the assistant said or recommended
- Game lore or mechanics
- Trade route prices or profit calculations
- Ship specifications or cargo capacities (those are ship facts, not user facts)

Distinguish between aUEC (in-game currency) and SCU (cargo units). Do NOT confuse them.

Every fact must reference specific names, numbers, or details from the conversation -- never use generic placeholders.

Output compact valid JSON on a single line. Example:
{"summary":"SUMMARY_HERE","facts":["FACT_1","FACT_2"]}
