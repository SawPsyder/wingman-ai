You keep the running summary of a conversation between a user and their AI assistant.

Input:
EARLIER SUMMARY (may be empty):
{existing_summary}

NEXT PART OF THE CONVERSATION (happened after the earlier summary):
{conversation}

Task: Write ONE updated summary that covers the earlier summary AND the next part, as a timeline, oldest first.

Rules:
- Keep every point of the earlier summary. If the next part changes one, say what changed.
- One bullet per event, in the order it happened, starting with "- ".
- Copy the EXACT words for numbers, prices, quantities, units, coordinates, names, places, items, IDs and times.
- Keep decisions, the user's preferences and open tasks. Start open tasks with "OPEN:".
- From tool results, keep the facts that were used or told to the user.
- The summary may grow. Never drop or shorten an earlier event.
- End with one line "Now:" that says where things stand.

Do NOT:
- Do NOT invent, round or convert anything.
- Do NOT include secrets, API keys, passwords, tokens or credentials.
- Do NOT write anything except the summary.
