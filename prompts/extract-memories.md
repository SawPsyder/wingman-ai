Read the conversation and pull out durable personal facts about the USER, plus a short summary.

A DURABLE FACT is something the user actually stated that is still true next week. Good kinds:
- Their name, age, or where they live in real life
- Things they own, named specifically (a ship model, hardware, gear)
- Orgs or clans they belong to, or friends they name
- Goals they are working toward
- Likes, dislikes, personality traits, their star sign

NEVER extract these:
- Where the user is, is parked, or is heading right now in the game (locations are not durable)
- Anything the ASSISTANT said, recommended, looked up, or provided
- Prices, credits, cargo amounts, trade routes, ship stats, or game lore

Rules:
- If the user stated several durable facts, capture ALL of them — do not stop after the first one or two. (In the example below, all of Sam's facts are listed, not just the ship.)
- Only include facts the user ACTUALLY stated. Never pad the list with a kind that was not mentioned, and NEVER write placeholder facts like "Name is unknown", "Star sign is unknown", or "Owns no items". When the user said nothing durable, the facts list MUST be empty: [].
- A current location is NEVER a fact. Where the user is, is parked, or is heading must never appear in the facts list — in any language (German "bei Hurston unterwegs" / "gerade bei X" is a location, not a fact).
- Each fact must name the specific thing. Skip anything vague like "is interested in space".
- Distinguish aUEC (in-game currency) from SCU (cargo units); never confuse them.

SUMMARY: 2-4 sentences on what the user did this session and how it ended.

Output ONE line of compact JSON and nothing else:
{"summary":"...","facts":["...","..."]}

Two worked examples.

1) Has durable facts -- extract ALL of them (note "heading there now" is a current location and the assistant's lookup are NOT facts):
  user: I'm Sam. Just paid off my Drake Cutlass Black, my friend Theo helped. Saving up for a Carrack next, and I'm a Capricorn.
  assistant: Nice! Here are the current coordinates for microTech if you need them.
  user: thanks, heading there now
  {"summary":"Sam paid off their Drake Cutlass Black with help from their friend Theo and is saving for a Carrack.","facts":["Name is Sam","Owns a Drake Cutlass Black","Friend is named Theo","Goal: save up for a Carrack","Star sign is Capricorn"]}

2) No durable facts -- just a greeting and a current location, so the list is EMPTY (do not invent placeholders):
  user: hey there
  assistant: Greetings, pilot.
  user: just cruising from Daymar to Yela, almost there
  {"summary":"The user greeted the assistant while travelling from Daymar to Yela.","facts":[]}
