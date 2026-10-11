"""Voice tools for the persistent location blocklist."""

from skills.uexcorp.uexcorp.tool.tool import Tool
from skills.uexcorp.uexcorp.tool.validator import Validator
from skills.uexcorp.uexcorp.location_blocklist import (
    CONFIRM_SECONDS,
    LocationBlocklist,
    format_expiry,
    parse_duration,
    signature,
)
from skills.uexcorp.uexcorp.location_resolver import format_candidates, resolve_location


def _store(helper) -> LocationBlocklist:
    return LocationBlocklist(helper)


def _confirm_or_preview(store, needed: bool, confirm: bool, sig: str, preview: str):
    if not needed:
        return None
    if confirm:
        if store.accept_confirm(sig):
            return None
        return (
            "That confirmation does not match the pending preview, or the preview expired. Nothing was saved.",
            "",
        )
    store.remember_preview(sig)
    return preview, ""


class ListLocationBlocks(Tool):
    TOOL_NAME = "uex_list_location_blocks"

    def execute(self, include_expired: bool | None = None) -> tuple[str, str]:
        store = _store(self._helper)
        now = store.now()
        shown = []
        for entry in store.read():
            expires = entry.get("expires")
            expired = bool(expires and int(expires) <= now)
            if expired and not include_expired:
                continue
            shown.append(
                {
                    "scope": entry.get("scope"),
                    "id": entry.get("id"),
                    "name": entry.get("name"),
                    "path": entry.get("path"),
                    "reason": entry.get("reason") or "",
                    "expires": format_expiry(expires),
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
            bits.append(f"{entry['terminal_count']} terminals")
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

    def execute(
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
        resolved = resolve_location(helper, name, scope)
        if resolved is None:
            return f"No location matches {name}. Nothing was saved.", ""
        if isinstance(resolved, list):
            return (
                "Several locations match. Nothing was saved. Ask which one, then call again with scope or the exact name:\n"
                + format_candidates(resolved),
                "",
            )
        store = _store(helper)
        count = store.terminal_count(resolved)
        needed = store.needs_confirm("block", resolved["scope"], count)
        sig = signature(
            "block",
            {
                "name": name,
                "scope": scope,
                "reason": reason or "",
                "duration": duration or "",
            },
        )
        preview = (
            f"Would block {resolved['scope']} {resolved['name']}"
            f"{(' in ' + resolved['path']) if resolved.get('path') else ''}: "
            f"{count} commodity terminals. "
            f"Ask the user, then call again with the same arguments and confirm=true "
            f"within {CONFIRM_SECONDS // 60} minutes."
        )
        held = _confirm_or_preview(store, needed, bool(confirm), sig, preview)
        if held:
            return held
        entry = store.add(resolved, reason, expires)
        when = format_expiry(entry.get("expires"))
        spoken = f"Blocked {entry['name']} ({entry['scope']})"
        if entry.get("path"):
            spoken += f" in {entry['path']}"
        spoken += f", {count} terminals."
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

    def execute(self, name: str, confirm: bool | None = None) -> tuple[str, str]:
        store = _store(self._helper)
        matches = store.matching_blocks(name)
        if not matches:
            resolved = resolve_location(self._helper, name, None)
            if isinstance(resolved, list):
                return (
                    "Several locations match. Nothing was changed:\n" + format_candidates(resolved),
                    "",
                )
            if isinstance(resolved, dict):
                matches = [
                    entry
                    for entry in store.active()
                    if entry.get("scope") == resolved["scope"] and entry.get("id") == resolved["id"]
                ]
        if not matches:
            return f"No block matches {name}.", ""
        needed = store.needs_confirm("unblock", removing=len(matches))
        sig = signature("unblock", {"name": name})
        names = ", ".join(entry.get("name") or "" for entry in matches)
        preview = (
            f"Would remove {len(matches)} blocks ({names}). "
            "Ask the user, then call again with the same name and confirm=true within 2 minutes."
        )
        held = _confirm_or_preview(store, needed, bool(confirm), sig, preview)
        if held:
            return held
        removed = store.remove_keys([(entry["scope"], int(entry["id"])) for entry in matches])
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

    def execute(self, confirm: bool | None = None, only_expired: bool | None = None) -> tuple[str, str]:
        store = _store(self._helper)
        only = bool(only_expired)
        entries = store.read()
        now = store.now()
        if only:
            targets = [entry for entry in entries if entry.get("expires") and int(entry["expires"]) <= now]
        else:
            targets = list(entries)
        sig = signature("clear", {"only_expired": only})
        preview = (
            f"Would clear {len(targets)} location block(s). "
            "Ask the user, then call again with the same arguments and confirm=true within 2 minutes."
        )
        held = _confirm_or_preview(store, True, bool(confirm), sig, preview)
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
