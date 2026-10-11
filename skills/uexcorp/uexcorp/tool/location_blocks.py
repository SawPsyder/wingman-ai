"""Voice tools for the persistent location blocklist."""

from skills.uexcorp.uexcorp.tool.tool import Tool
from skills.uexcorp.uexcorp.tool.validator import Validator
from skills.uexcorp.uexcorp.location_blocklist import (
    CONFIRM_SECONDS,
    LocationBlocklist,
    _expiry_value,
    _ids_equal,
    _parse_block_ref,
    coverage_phrase,
    format_expiry,
    parse_duration,
    signature,
)
from skills.uexcorp.uexcorp.resolver import format_candidates, resolve_location


def _store(helper) -> LocationBlocklist:
    return LocationBlocklist(helper)


# Agreement counts in any language. The words are in the question so a small
# model treats ja/jo/passt/mach the same as yes.
_AGREEMENT_QUESTION = (
    "Did the user agree to the pending location-block preview? "
    "Count agreement in any language. These are agreement: yes, ok, ja, jo, passt, mach. "
    "A refusal in any language is not agreement."
)


async def _confirm_or_preview(store, helper, needed: bool, confirm: bool, sig: str, preview: str):
    if not needed:
        return None
    if confirm:
        if await _user_declined(helper):
            return "The last reply declined that preview. Nothing was saved.", ""
        if store.accept_confirm(sig):
            return None
        return (
            "That confirmation does not match the pending preview, or the preview expired. Nothing was saved.",
            "",
        )
    store.remember_preview(sig)
    return preview, ""


async def _user_declined(helper) -> bool:
    """Confident System One 'no' on the last user line. Unavailable means confirm stands."""
    try:
        wingman = helper.get_handler_config().get_wingman()
        system_one = getattr(wingman, "system_one", None)
        if not system_one or not system_one.available:
            return False
        conversation = getattr(wingman, "conversation", None)
        history = conversation.history() if conversation is not None else []
        last = ""
        for message in reversed(list(history or [])):
            role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
            if role == "user":
                content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
                last = content or ""
                break
        if not str(last).strip():
            return False
        answers = await system_one.decide(
            {"message": last},
            {"agreed": system_one.noul(_AGREEMENT_QUESTION)},
        )
        probability = answers.noul("agreed")
        if probability is not None and probability <= 0.2:
            return True
    except Exception:
        return False
    return False


def _candidate_reply(resolved) -> tuple[str, str]:
    listing = format_candidates(resolved.candidates)
    if len(resolved.candidates) == 1:
        hit = resolved.candidates[0]
        return (
            f"Nothing was saved. {hit.get('label') or hit['name']} is not an exact match. "
            f"Ask which place, then call again with the exact name:\n{listing}",
            "",
        )
    return (
        "Several locations match. Nothing was saved. Ask which one, then call again with scope or the exact name:\n"
        + listing,
        "",
    )


def _covering_parent(store, candidate):
    parents = candidate.get("parents") or {}
    for entry in store.active():
        parent_id = parents.get(entry.get("scope"))
        if parent_id is not None and _ids_equal(parent_id, entry.get("id")):
            return entry
    return None


class ListLocationBlocks(Tool):
    TOOL_NAME = "uex_list_location_blocks"

    async def execute(self, include_expired: bool | None = None) -> tuple[str, str]:
        store = _store(self._helper)
        now = store.now()
        shown = []
        for entry in store.read():
            expires = entry.get("expires")
            stamp = _expiry_value(expires)
            expired = bool(stamp and stamp <= now)
            if expired and not include_expired:
                continue
            shown.append(
                {
                    "scope": entry.get("scope"),
                    "id": entry.get("id"),
                    "name": entry.get("name"),
                    "path": entry.get("path"),
                    "reason": entry.get("reason") or "",
                    "expires": format_expiry(expires, now=now),
                    "expired": expired,
                    "stale": bool(entry.get("stale")),
                    "terminal_count": store.terminal_count(entry),
                }
            )
        if not shown:
            return "No location blocks.", ""
        lines = []
        for entry in shown:
            bits = [f"{entry['name']} ({entry['scope']})"]
            if entry["path"]:
                bits.append(entry["path"])
            if entry["reason"]:
                bits.append(entry["reason"])
            if entry["expires"]:
                bits.append(f"until {entry['expires']}")
            if entry["expired"]:
                bits.append("expired")
            if entry["stale"]:
                bits.append("stale")
            counted = entry["terminal_count"]
            commodity_word = "commodity terminal" if counted == 1 else "commodity terminals"
            bits.append(f"{counted} {commodity_word}")
            lines.append("; ".join(bits))
        return "\n".join(lines), ""

    def get_optional_fields(self) -> dict[str, Validator]:
        return {
            "include_expired": Validator(
                Validator.VALIDATE_BOOL,
                prompt="When true, include blocks whose expiry has passed.",
            )
        }

    def get_description(self) -> str:
        return "List persistent location blocks, with scope, reason, expiry, and how many terminals each one covers."

    def get_prompt(self) -> str:
        return self.get_description()


class BlockLocation(Tool):
    TOOL_NAME = "uex_block_location"

    async def execute(
        self,
        name: str,
        scope: str | None = None,
        reason: str | None = None,
        duration: str | None = None,
        confirm: bool | None = None,
    ) -> tuple[str, str]:
        helper = self._helper
        try:
            expires = parse_duration(duration, helper.get_timestamp())
        except ValueError as error:
            return str(error), ""
        resolved = await resolve_location(helper, name, scope, for_write=True, available_only=False)
        if resolved.match is None and not resolved.candidates:
            return f"No location matches {name}. Nothing was saved.", ""
        if resolved.match is None:
            return _candidate_reply(resolved)
        place = resolved.match
        store = _store(helper)
        count = store.terminal_count(place)
        total = store.terminal_total(place)
        non_exact = resolved.method != "exact"
        needed = store.needs_confirm("block", place["scope"], count) or non_exact
        sig = signature(
            "block",
            {
                "name": name,
                "scope": scope,
                "reason": reason or "",
                "duration": duration or "",
            },
        )
        where = f" in {place['path']}" if place.get("path") else ""
        preview = (
            f"Would block {place['scope']} {place['name']}{where}: "
            f"{coverage_phrase(place['name'], count, total)}. "
        )
        lagrange = store.lagrange_sentence(place)
        if lagrange:
            preview += lagrange + " "
        preview += (
            f"Ask the user, then call again with the same arguments and confirm=true "
            f"within {CONFIRM_SECONDS // 60} minutes."
        )
        held = await _confirm_or_preview(store, helper, needed, bool(confirm), sig, preview)
        if held:
            return held
        entry = store.add(place, reason, expires)
        if entry is None:
            return "The location blocklist could not be saved.", ""
        when = format_expiry(entry.get("expires"), now=store.now())
        spoken = f"Blocked {entry['name']} ({entry['scope']})"
        if entry.get("path"):
            spoken += f" in {entry['path']}"
        spoken += f", {coverage_phrase(entry['name'], count, total)}."
        if entry.get("reason"):
            spoken += f" Reason: {entry['reason']}."
        if when:
            spoken += f" Until {when}."
        return spoken, ""

    def get_mandatory_fields(self) -> dict[str, Validator]:
        return {
            "name": Validator(
                Validator.VALIDATE_STRING,
                prompt="Location name to block, such as Levski or Pyro.",
            )
        }

    def get_optional_fields(self) -> dict[str, Validator]:
        from skills.uexcorp.uexcorp.location_blocklist import SCOPES

        return {
            "scope": Validator(
                Validator.VALIDATE_ENUM,
                config={"enum": list(SCOPES)},
                prompt="Limit the match to one scope: system, planet, orbit, moon, city, station, outpost, poi, or terminal.",
            ),
            "reason": Validator(
                Validator.VALIDATE_STRING,
                prompt="Optional reason, repeated when a route hits this block.",
            ),
            "duration": Validator(
                Validator.VALIDATE_STRING,
                prompt="Optional expiry such as 2h, 3 days, or 1 week. Omit for a block that stays until removed.",
            ),
            "confirm": Validator(
                Validator.VALIDATE_BOOL,
                prompt="Set true only on the follow-up call after the user agrees to a preview.",
            ),
        }

    def get_description(self) -> str:
        return (
            "Block a location from trade routes. A whole system or planet, or a block covering more than 10 terminals, "
            "returns a preview first. Call again with the same arguments and confirm=true within 2 minutes to save it. "
            "An ambiguous name saves nothing and lists candidates."
        )

    def get_prompt(self) -> str:
        return self.get_description()


class UnblockLocation(Tool):
    TOOL_NAME = "uex_unblock_location"

    async def execute(self, name: str, confirm: bool | None = None) -> tuple[str, str]:
        helper = self._helper
        store = _store(helper)
        matches = store.matching_blocks(name)
        non_exact = False
        # scope:id is exact. A missing station:52 must not fuzzy-match Stanton.
        if _parse_block_ref(name) is not None:
            if not matches:
                return f"No block matches {name}.", ""
        elif not matches:
            resolved = await resolve_location(helper, name, None, for_write=True, available_only=False)
            if resolved.match is None and resolved.candidates:
                return _candidate_reply(resolved)
            if resolved.match is not None:
                non_exact = resolved.method != "exact"
                place = resolved.match
                matches = [
                    entry
                    for entry in store.read()
                    if entry.get("scope") == place["scope"] and _ids_equal(entry.get("id"), place["id"])
                ]
                if not matches:
                    parent = _covering_parent(store, place)
                    if parent:
                        return (
                            f"{place['name']} is covered by the {parent.get('scope')} block {parent.get('name')}. "
                            "Remove that block to unblock it.",
                            "",
                        )
        if not matches:
            return f"No block matches {name}.", ""
        needed = store.needs_confirm("unblock", removing=len(matches)) or non_exact
        sig = signature("unblock", {"name": name})
        names = ", ".join(entry.get("name") or "" for entry in matches)
        preview = (
            f"Would remove {len(matches)} blocks ({names}). "
            "Ask the user, then call again with the same name and confirm=true within 2 minutes."
        )
        held = await _confirm_or_preview(store, helper, needed, bool(confirm), sig, preview)
        if held:
            return held
        removed = store.remove_keys([(entry["scope"], entry["id"]) for entry in matches])
        return f"Removed {removed} block(s): {names}.", ""

    def get_mandatory_fields(self) -> dict[str, Validator]:
        return {
            "name": Validator(
                Validator.VALIDATE_STRING,
                prompt="Block name, or scope:id from uex_list_location_blocks, such as terminal:12.",
            )
        }

    def get_optional_fields(self) -> dict[str, Validator]:
        return {
            "confirm": Validator(
                Validator.VALIDATE_BOOL,
                prompt="Required when the name matches more than one block. Set true only after the user agrees.",
            )
        }

    def get_description(self) -> str:
        return "Remove one location block. If the name matches several blocks, the first call is a preview."

    def get_prompt(self) -> str:
        return self.get_description()


class ClearLocationBlocks(Tool):
    TOOL_NAME = "uex_clear_location_blocks"

    async def execute(self, confirm: bool | None = None, only_expired: bool | None = None) -> tuple[str, str]:
        store = _store(self._helper)
        only = bool(only_expired)
        entries = store.read()
        now = store.now()
        if only:
            targets = [
                entry
                for entry in entries
                if _expiry_value(entry.get("expires")) and _expiry_value(entry.get("expires")) <= now
            ]
        else:
            targets = list(entries)
        sig = signature("clear", {"only_expired": only})
        preview = (
            f"Would clear {len(targets)} location block(s). "
            "Ask the user, then call again with the same arguments and confirm=true within 2 minutes."
        )
        held = await _confirm_or_preview(store, self._helper, True, bool(confirm), sig, preview)
        if held:
            return held
        removed = store.clear(only_expired=only)
        if only:
            return f"Cleared {removed} expired block(s).", ""
        return f"Cleared {removed} location block(s).", ""

    def get_optional_fields(self) -> dict[str, Validator]:
        return {
            "confirm": Validator(
                Validator.VALIDATE_BOOL,
                prompt="Set true only after the user agrees to the preview.",
            ),
            "only_expired": Validator(
                Validator.VALIDATE_BOOL,
                prompt="When true, remove only blocks whose expiry has passed.",
            ),
        }

    def get_description(self) -> str:
        return "Clear location blocks. The first call returns a preview and saves nothing until confirm=true."

    def get_prompt(self) -> str:
        return self.get_description()
