"""Realistic simulated conversations for exercising Persistent Memory.

Each ``Scenario`` is a believable multi-turn session a real Wingman user might
have. The labels describe what a *good* memory system should do with it:

  expect_facts  - durable facts that SHOULD end up stored (extraction recall).
                  Each entry is a list of synonyms; the concept counts as
                  captured if any synonym appears in any stored fact.
  forbid_facts  - substrings that must NOT be stored (transient locations,
                  prices, assistant statements). These are the classic false
                  positives — a current location is never a durable fact.
  at_most_one   - concepts that should occupy at most ONE stored fact, even if
                  mentioned several times (dedup quality).
  recall        - follow-up questions a user would ask later; checks that
                  ``build_memory_context`` resurfaces the right facts (this
                  exercises the EMBED model + similarity threshold, which the
                  prompt-level evals never touched).
  forget        - "forget X" requests; checks forget_by_query hits the right row.
  edits         - user edits a memory then asks again; checks update + re-recall.

Conversations deliberately mix durable facts with lots of transient chatter so
the suite measures precision, not just recall.

Categories: "star_citizen" (the priority), "other_game", "assistant".
"""

from dataclasses import dataclass, field


@dataclass
class RecallProbe:
    """A follow-up question; passes if every concept in expect_any is surfaced."""

    query: str
    expect_any: list[list[str]] = field(default_factory=list)
    expect_absent: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class ForgetProbe:
    """A 'forget X' request; passes if the matching fact is gone afterwards."""

    query: str
    expect_gone: list[str]
    expect_kept: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class EditProbe:
    """User edits a stored fact, then asks about it again."""

    find_query: str          # locate the fact to edit (best similarity match)
    new_content: str         # the corrected fact text
    recall_query: str        # question asked afterwards
    expect_any: list[list[str]] = field(default_factory=list)
    expect_absent: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Scenario:
    id: str
    title: str
    category: str
    messages: list[dict]
    expect_facts: list[list[str]] = field(default_factory=list)
    forbid_facts: list[str] = field(default_factory=list)
    at_most_one: list[list[str]] = field(default_factory=list)
    recall: list[RecallProbe] = field(default_factory=list)
    forget: list[ForgetProbe] = field(default_factory=list)
    edits: list[EditProbe] = field(default_factory=list)


def _m(role, content):
    return {"role": role, "content": content}


# ── Star Citizen: a long play session (the priority case) ────────────────

SC_LONG = Scenario(
    id="sc_long_session",
    title="Star Citizen — long mining session",
    category="star_citizen",
    messages=[
        _m("user", "Hey, I'm back. Name's Ben by the way, never properly introduced myself."),
        _m("assistant", "Good to have you back, Ben. What's the plan today?"),
        _m("user", "Mining run. I picked up a Drake Cutlass Black last week and I fly a Prospector for solo mining."),
        _m("assistant", "The Prospector is a great little miner. Quantanium pays well right now."),
        _m("user", "I'm parked at Lorville at the moment, about to push out to Daymar."),
        _m("assistant", "Daymar has solid deposits. Mind the wrecks near the rocks."),
        _m("user", "My org Shadow Syndicate runs ops out there on weekends. I usually roll with my buddy Mara."),
        _m("assistant", "Sounds like a tight crew."),
        _m("user", "Honestly I love mining but I can't stand PvP — I always lose my ship and rage quit."),
        _m("assistant", "Mining's the peaceful grind. Stick with what you enjoy."),
        _m("user", "Quantanium is sitting around 27 aUEC a unit right now, not bad."),
        _m("assistant", "Decent. Sell before it starts to decay."),
        _m("user", "Long term I'm grinding toward a Carrack. That's the dream ship for me."),
        _m("assistant", "A worthy goal. You'll get there with runs like this."),
        _m("user", "Alright, I'm at Daymar now, scanning the first rock. Catch you later."),
        _m("assistant", "Good luck out there, Ben."),
    ],
    expect_facts=[
        ["name is ben", "ben"],
        ["cutlass"],
        ["prospector"],
        ["shadow syndicate", "syndicate"],
        ["mara"],
        ["mining", "mine", "miner"],
        ["pvp"],
        ["carrack"],
    ],
    forbid_facts=["lorville", "daymar", "27", "auec", "parked", "decay"],
    at_most_one=[["syndicate"], ["carrack"]],
    recall=[
        RecallProbe("What ships do I own?", [["cutlass", "prospector"]],
                    note="owned ships should resurface"),
        RecallProbe("Who do I usually play with?", [["mara"]], ),
        RecallProbe("What org am I in?", [["syndicate", "shadow"]]),
        RecallProbe("What am I saving up for?", [["carrack"]]),
        RecallProbe("What do I enjoy doing in the game?", [["mining", "mine"]]),
        RecallProbe("Where am I right now?", expect_absent=["lorville", "daymar"],
                    note="locations must never have been stored"),
    ],
    forget=[
        ForgetProbe("forget that I'm in Shadow Syndicate",
                    expect_gone=["syndicate", "shadow"],
                    expect_kept=["carrack"]),
    ],
    edits=[
        EditProbe(find_query="the Carrack I'm saving for",
                  new_content="Goal: save up for a Polaris",
                  recall_query="What ship am I saving for?",
                  expect_any=[["polaris"]], expect_absent=["carrack"],
                  note="user changed their goal ship"),
    ],
)

# ── Star Citizen: a combat / org evening (different flavour) ──────────────

SC_COMBAT = Scenario(
    id="sc_combat_night",
    title="Star Citizen — bounty hunting night",
    category="star_citizen",
    messages=[
        _m("user", "Evening. It's Priya — we spoke a while back."),
        _m("assistant", "Welcome back, Priya. Ready for some action?"),
        _m("user", "Bounty night. I main a Vanguard Warden and I just unlocked my Hornet again."),
        _m("assistant", "The Warden's a tank. Good for ERT bounties."),
        _m("user", "Yeah, my org Iron Vultures does Very Hard targets every Friday."),
        _m("assistant", "Coordinated firepower makes those manageable."),
        _m("user", "I really enjoy dogfighting, but I hate cargo hauling — bores me to tears."),
        _m("assistant", "Then you're in the right line of work."),
        _m("user", "Heading to Crusader to stage now, sitting at 480k aUEC after repairs."),
        _m("assistant", "Plenty for ammo and a respawn or two."),
        _m("user", "My goal this patch is to hit the top 100 on the bounty leaderboard with my friend Dex."),
        _m("assistant", "Ambitious. Fly smart and you've got a shot."),
    ],
    expect_facts=[
        ["priya"],
        ["vanguard", "warden"],
        ["hornet"],
        ["iron vultures", "vultures"],
        ["dogfight", "combat", "bounty"],
        ["cargo", "hauling"],   # dislike
        ["dex"],
        ["leaderboard", "top 100", "top 100"],
    ],
    forbid_facts=["crusader", "480k", "480", "auec", "repairs", "friday"],
    at_most_one=[["vultures"]],
    recall=[
        RecallProbe("What's my main ship?", [["vanguard", "warden"]]),
        RecallProbe("What do I hate doing?", [["cargo", "hauling"]]),
        RecallProbe("Who's my wingmate?", [["dex"]]),
        RecallProbe("What's my org?", [["vultures", "iron"]]),
    ],
)

# ── Another game: Elite Dangerous ────────────────────────────────────────

ELITE = Scenario(
    id="elite_explorer",
    title="Elite Dangerous — exploration session",
    category="other_game",
    messages=[
        _m("user", "Hi, I'm Tomas. Just getting into a long exploration trip."),
        _m("assistant", "Greetings, Commander Tomas. Where are you headed?"),
        _m("user", "Out to Colonia in my Asp Explorer. I named her the Wandering Star."),
        _m("assistant", "The Asp is a classic explorer's choice."),
        _m("user", "I'm a member of the Deep Space Network squadron."),
        _m("assistant", "A fine community of explorers."),
        _m("user", "Currently 3,200 light years out, fuel scooping at a white dwarf."),
        _m("assistant", "Careful near the exclusion zone."),
        _m("user", "I love finding earth-likes, but I despise combat — I'm a pacifist out here."),
        _m("assistant", "Exploration suits the peaceful type."),
        _m("user", "Goal is to reach Sagittarius A* before the next community goal ends."),
        _m("assistant", "The galactic core is a sight worth the trip."),
    ],
    expect_facts=[
        ["tomas"],
        ["asp", "explorer", "wandering star"],
        ["deep space network", "squadron"],
        ["earth-like", "exploration", "explore"],
        ["combat"],  # dislike
        ["sagittarius", "galactic core", "core"],
    ],
    forbid_facts=["colonia", "3,200", "3200", "light years", "white dwarf", "fuel"],
    recall=[
        RecallProbe("What ship do I fly?", [["asp", "wandering star"]]),
        RecallProbe("What's my squadron?", [["deep space", "network"]]),
        RecallProbe("What's my goal?", [["sagittarius", "core"]]),
    ],
)

# ── Outside games: desktop assistant ─────────────────────────────────────

ASSISTANT = Scenario(
    id="assistant_desktop",
    title="Desktop assistant — real-life facts",
    category="assistant",
    messages=[
        _m("user", "Morning. I'm Sarah, I work as a backend developer in Munich."),
        _m("assistant", "Good morning, Sarah. How can I help today?"),
        _m("user", "Mostly I need help staying organized. I'm learning Rust on the side."),
        _m("assistant", "Rust is a great language to pick up."),
        _m("user", "I prefer dark mode everywhere and I'm a coffee-before-anything person."),
        _m("assistant", "Noted — function follows caffeine."),
        _m("user", "Can you set a reminder for my dentist at 3pm today?"),
        _m("assistant", "I've noted the 3pm reminder."),
        _m("user", "My main goal this quarter is to ship our new payments API."),
        _m("assistant", "A clear target. We can break it down whenever you like."),
    ],
    expect_facts=[
        ["sarah"],
        ["backend developer", "developer", "munich"],
        ["rust"],
        ["dark mode"],
        ["coffee"],
        ["payments api", "payments"],
    ],
    forbid_facts=["3pm", "dentist", "reminder", "today", "this quarter"],
    recall=[
        RecallProbe("What do I do for work?", [["developer", "backend", "munich"]]),
        RecallProbe("What am I learning?", [["rust"]]),
        RecallProbe("What's my goal this quarter?", [["payments"]]),
    ],
)

# ── Pure small talk: nothing durable (precision guard) ───────────────────

EMPTY_CHAT = Scenario(
    id="smalltalk_empty",
    title="Small talk — no durable facts",
    category="star_citizen",
    messages=[
        _m("user", "hey there"),
        _m("assistant", "Greetings, pilot."),
        _m("user", "just cruising from Daymar to Yela, almost there"),
        _m("assistant", "Smooth flying."),
        _m("user", "what's the time to Yela again?"),
        _m("assistant", "About six minutes at quantum speed."),
        _m("user", "cool thanks, nothing else really"),
        _m("assistant", "Safe travels."),
    ],
    expect_facts=[],
    forbid_facts=["daymar", "yela", "six minutes", "cruising", "quantum"],
)

# ── German session: cross-lingual extraction ─────────────────────────────

GERMAN = Scenario(
    id="german_session",
    title="Star Citizen — German session",
    category="star_citizen",
    messages=[
        _m("user", "Hallo, ich heiße Lukas und fliege eine Avenger Titan."),
        _m("assistant", "Schön dich kennenzulernen, Lukas."),
        _m("user", "Ich bin in der Organisation Sternenjäger und mein Freund Jonas spielt mit mir."),
        _m("assistant", "Klingt nach einem guten Team."),
        _m("user", "Ich mag Frachttransport, aber Bergbau finde ich langweilig."),
        _m("assistant", "Jeder hat seine Vorlieben."),
        _m("user", "Ich bin gerade bei Hurston unterwegs, gleich bei der Raffinerie."),
        _m("assistant", "Pass auf den Verkehr auf."),
        _m("user", "Mein Ziel ist es, eine Caterpillar zu kaufen."),
        _m("assistant", "Ein großes Schiff für große Träume."),
    ],
    expect_facts=[
        ["lukas"],
        ["avenger", "titan"],
        ["sternenjäger", "sternenjager"],
        ["jonas"],
        ["fracht", "cargo", "hauling", "transport"],
        ["caterpillar"],
    ],
    forbid_facts=["hurston", "raffinerie", "refinery", "unterwegs"],
    recall=[
        RecallProbe("Welches Schiff fliege ich?", [["avenger", "titan"]]),
        RecallProbe("Was ist mein Ziel?", [["caterpillar"]]),
    ],
)


# ── Star Citizen: the brutal one — long VA session, STT noise, skills ────
# Simulates a real player with Voice Activation always on: half-sentences, the
# mic catching room noise and self-talk, STT mishearings ("cut less", "lore
# ville", "Houston" for Hurston, "a wreck" for aUEC), skill/MCP tool calls
# (ship loadout, UEX trade, reminder, mobiGlas), transient locations/prices/cargo
# from those tools — with ~12 durable facts scattered across 46 turns. This is
# the stress test: can the model find the signal buried in all that bloat?

SC_VA_MARATHON = Scenario(
    id="sc_va_marathon",
    title="Star Citizen — voice-activation marathon (STT noise + skills)",
    category="star_citizen",
    messages=[
        _m("user", "Okay okay I'm in. Comms check — you hearing me?"),
        _m("assistant", "Loud and clear, pilot."),
        _m("user", "Right. So uh, where was I. Hold on, let me spawn the ship."),
        _m("user", "Pulling out my Constellation Andromeda. God I love this thing."),
        _m("assistant", "The Constellation Andromeda, a fine choice. Want me to pull the loadout?"),
        _m("user", "Yeah run the uh, the ship loadout skill, show me my components."),
        _m("assistant", "(ship loadout skill) Your Andromeda has size-4 shields and four S5 weapons mounted."),
        _m("user", "nice nice nice. wait no not you mom, I'm on the game. sorry about that."),
        _m("user", "Anyway it's me, Marcus, by the way — never said hi properly."),
        _m("assistant", "Good to fly with you, Marcus."),
        _m("user", "I also keep a cut less, ugh, a Cutlass Black for the smaller runs."),
        _m("assistant", "Noted — a Cutlass Black as your secondary."),
        _m("user", "Where's the... okay quantum spooling. Heading to Crusader, gimme a sec."),
        _m("user", "ugh the tutorial guy at lore ville never shuts up"),
        _m("assistant", "Spooling complete. Safe travels to Crusader."),
        _m("user", "So my org, Midnight Reapers, we've got bounty night tonight."),
        _m("assistant", "Midnight Reapers — sounds like a serious crew."),
        _m("user", "yeah me and Vince and Lena basically run the bounty wing together."),
        _m("assistant", "A solid trio."),
        _m("user", "honestly bounty hunting is the best, and I love a good salvage run too."),
        _m("assistant", "Combat and salvage — you like the hands-on work."),
        _m("user", "can't STAND trading though, hauling boxes around bores me to death."),
        _m("assistant", "Then we'll keep you out of the cargo lanes."),
        _m("user", "hey what's quantanium going for right now? check the trade thing."),
        _m("assistant", "(UEX trade skill) Quantanium is selling at ArcCorp 027 for about 28 a unit."),
        _m("user", "a wreck per unit, not bad. wait, aUEC, you know what I mean."),
        _m("assistant", "Indeed — 28 aUEC per unit."),
        _m("user", "how much cargo does the Connie hold again?"),
        _m("assistant", "(ship loadout skill) The Constellation Andromeda carries 96 SCU."),
        _m("user", "right right. okay forget the trading, that's boring anyway."),
        _m("user", "set me a reminder to refuel in ten minutes, will ya?"),
        _m("assistant", "(reminder skill) Reminder set: refuel in ten minutes."),
        _m("user", "perfect. open my mobiGlas real quick."),
        _m("assistant", "Opening your mobiGlas."),
        _m("user", "ugh where did that pirate go. come on. scanning, scanning."),
        _m("user", "Houston traffic control is a nightmare today, so many ships out here."),
        _m("assistant", "Stay sharp out there."),
        _m("user", "you know my actual dream? becoming wing commander of the Reapers someday."),
        _m("assistant", "A worthy ambition, Marcus."),
        _m("user", "and one day, ONE day, I'm buying a Javelin. a whole capital ship, man."),
        _m("assistant", "Dream big. The Javelin is the ultimate goal."),
        _m("user", "for now I'm grinding. oh by the way I'm playing from Hamburg, it's like 1am here."),
        _m("assistant", "Late night in Hamburg — respect the dedication."),
        _m("user", "okay engaging the pirate, here we go, weapons hot. talk after."),
        _m("assistant", "Good hunting, Marcus."),
        _m("user", "got him! okay logging off for tonight. later."),
    ],
    expect_facts=[
        ["marcus", "markus"],
        ["constellation", "andromeda"],
        ["cutlass"],
        ["midnight reapers", "reapers"],
        ["vince"],
        ["lena"],
        ["bounty"],
        ["salvage"],
        ["trading", "hauling", "haul"],   # the dislike
        ["wing commander", "commander"],
        ["javelin"],
        ["hamburg"],
    ],
    forbid_facts=[
        "lore ville", "lorville", "crusader", "arccorp", "houston", "mobiglas",
        "quantanium", "scu", "auec", "96", "refuel", "reminder",
    ],
    at_most_one=[["reapers"], ["constellation"], ["cutlass"]],
    recall=[
        RecallProbe("What ships do I own?", [["constellation", "cutlass", "andromeda"]]),
        RecallProbe("What's my org?", [["reapers", "midnight"]]),
        RecallProbe("Who do I fly with?", [["vince", "lena"]]),
        RecallProbe("What do I enjoy doing?", [["bounty", "salvage"]]),
        RecallProbe("What do I dislike?", [["trading", "hauling"]]),
        RecallProbe("What's my big dream ship?", [["javelin"]]),
        RecallProbe("Where do I live in real life?", [["hamburg"]]),
        RecallProbe("Where am I flying right now?",
                    expect_absent=["lorville", "lore ville", "crusader", "arccorp"],
                    note="in-game locations must never have been stored"),
    ],
    forget=[
        ForgetProbe("forget that I'm in Midnight Reapers",
                    expect_gone=["reapers", "midnight"],
                    expect_kept=["javelin"]),
    ],
    edits=[
        EditProbe(find_query="the Javelin I want to buy",
                  new_content="Goal: save up for an Idris frigate",
                  recall_query="What capital ship am I saving for?",
                  expect_any=[["idris"]], expect_absent=["javelin"],
                  note="player changed their dream ship"),
    ],
)


SCENARIOS = [SC_LONG, SC_COMBAT, ELITE, ASSISTANT, EMPTY_CHAT, GERMAN, SC_VA_MARATHON]


def get_scenarios(ids=None, categories=None):
    """Filter the scenario set by id substrings and/or categories."""
    out = SCENARIOS
    if categories:
        out = [s for s in out if s.category in categories]
    if ids:
        out = [s for s in out if any(i in s.id for i in ids)]
    return out
