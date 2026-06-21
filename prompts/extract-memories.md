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
- Scan EVERY user message from the first to the last before answering. Facts are spread across the whole conversation, not just the opening — a ship named in the third message and a goal in the tenth BOTH count. Capture ALL of them; a typical session has five to eight. Do not stop after the first one or two.
- Only include facts the user ACTUALLY stated. Never pad the list with a kind that was not mentioned, and NEVER write placeholder facts like "Name is unknown", "Star sign is unknown", or "Owns no items". When the user said nothing durable, the facts list MUST be empty: [].
- A current location is NEVER a fact. Where the user is, is parked, or is heading must never appear in the facts list — in any language (German "bei Hurston unterwegs" / "gerade bei X" is a location, not a fact).
- Each fact must name the specific thing. Skip anything vague like "is interested in space".
- Distinguish aUEC (in-game currency) from SCU (cargo units); never confuse them.

SUMMARY: 2-4 sentences on what the user did this session and how it ended.

Output ONE line of compact JSON and nothing else:
{"summary":"...","facts":["...","..."]}

Two worked examples.

1) Facts are spread across many turns -- scan the WHOLE conversation and extract every one (note "parked at New Babbage" is a current location and the assistant's lines are NOT facts):
  user: Hey, I'm Mia.
  assistant: Good to see you, Mia.
  user: I finally bought a Drake Cutlass Black.
  assistant: A solid ship.
  user: I'm parked at New Babbage right now though.
  assistant: Safe travels.
  user: My org is the Red Foxes and I usually fly with my friend Leo.
  assistant: Sounds like a good crew.
  user: I love salvage runs but I can't stand mining. Long term I'm saving up for a Reclaimer.
  {"summary":"Mia bought a Drake Cutlass Black, flies with her org the Red Foxes and her friend Leo, enjoys salvage but dislikes mining, and is saving for a Reclaimer.","facts":["Name is Mia","Owns a Drake Cutlass Black","Member of the Red Foxes org","Friend is named Leo","Enjoys salvage runs","Dislikes mining","Goal: save up for a Reclaimer"]}

2) No durable facts -- just a greeting and a current location, so the list is EMPTY (do not invent placeholders):
  user: hey there
  assistant: Greetings, pilot.
  user: just cruising from Daymar to Yela, almost there
  {"summary":"The user greeted the assistant while travelling from Daymar to Yela.","facts":[]}
