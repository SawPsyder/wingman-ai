"""Persistent location blocklist.

The yaml file is the only record. Imports must not drop or rewrite it when
the import failed or the location tables are empty. Expired rows stop
applying immediately and are removed on the next write.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import yaml

from skills.uexcorp.uexcorp.handler.config_handler import blacklist_flag, blacklist_id
from skills.uexcorp.uexcorp.log import warn

CONFIRM_SECONDS = 120
TERMINAL_CONFIRM_LIMIT = 10
MIGRATED_REASON = "migrated"

# orbit is included because terminals carry id_orbit. poi stays from the
# concept's scope list.
SCOPES = (
    "system",
    "planet",
    "orbit",
    "moon",
    "city",
    "station",
    "outpost",
    "poi",
    "terminal",
)

# scope -> (table, terminal column, alias columns, path name columns, parent id columns)
_SCOPE = {
    "system": ("star_system", "id_star_system", ("code",), (), {}),
    "planet": ("planet", "id_planet", ("code",), ("star_system_name",), {"system": "id_star_system"}),
    "orbit": ("orbit", "id_orbit", ("code",), ("star_system_name",), {"system": "id_star_system"}),
    "moon": (
        "moon",
        "id_moon",
        ("code",),
        ("star_system_name", "planet_name"),
        {"system": "id_star_system", "planet": "id_planet", "orbit": "id_orbit"},
    ),
    "city": (
        "city",
        "id_city",
        ("code",),
        ("star_system_name", "planet_name"),
        {"system": "id_star_system", "planet": "id_planet", "orbit": "id_orbit", "moon": "id_moon"},
    ),
    "station": (
        "space_station",
        "id_space_station",
        ("nickname",),
        ("star_system_name", "planet_name"),
        {
            "system": "id_star_system",
            "planet": "id_planet",
            "orbit": "id_orbit",
            "moon": "id_moon",
            "city": "id_city",
        },
    ),
    "outpost": (
        "outpost",
        "id_outpost",
        ("nickname",),
        ("star_system_name", "planet_name"),
        {"system": "id_star_system", "planet": "id_planet", "orbit": "id_orbit", "moon": "id_moon"},
    ),
    "poi": (
        "poi",
        "id_poi",
        ("nickname",),
        ("star_system_name", "planet_name"),
        {
            "system": "id_star_system",
            "planet": "id_planet",
            "orbit": "id_orbit",
            "moon": "id_moon",
            "station": "id_space_station",
            "outpost": "id_outpost",
            "city": "id_city",
        },
    ),
    "terminal": (
        "terminal",
        "id",
        ("nickname", "code"),
        ("star_system_name", "planet_name", "space_station_name", "city_name", "moon_name", "outpost_name"),
        {
            "system": "id_star_system",
            "planet": "id_planet",
            "orbit": "id_orbit",
            "moon": "id_moon",
            "city": "id_city",
            "station": "id_space_station",
            "outpost": "id_outpost",
            "poi": "id_poi",
        },
    ),
}

_DURATION = re.compile(
    r"^\s*(\d+)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)\s*$",
    re.IGNORECASE,
)
_DURATION_SECONDS = {
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 7 * 86400,
    "week": 7 * 86400,
    "weeks": 7 * 86400,
}

# path -> {"signature": str, "until": int}
_PREVIEWS: dict[str, dict] = {}


def config_dir(helper) -> str:
    from services.file import get_writable_dir

    return get_writable_dir(os.path.join(helper.get_data_path(), "config"))


def parse_duration(text: str | None, now: int) -> int | None:
    """Return an absolute expiry, or None when the block does not expire."""
    if text is None or str(text).strip() == "":
        return None
    match = _DURATION.match(str(text))
    if not match:
        raise ValueError(f"Unrecognised duration {text!r}. Use 30m, 2h, 3 days, or 1 week.")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    return int(now) + amount * _DURATION_SECONDS[unit]


def _falsey_expiry(value):
    if isinstance(value, str) and not blacklist_flag(value):
        return None
    if value in (None, False):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class LocationBlocklist:
    def __init__(self, helper):
        self.helper = helper
        self.path = os.path.join(config_dir(helper), "location_blocklist.yaml")
        self._legacy = os.path.join(config_dir(helper), "terminal_blacklist.yaml")

    def now(self) -> int:
        return int(self.helper.get_timestamp())

    def read(self) -> list[dict]:
        self.migrate()
        return self._read_file()

    def active(self) -> list[dict]:
        now = self.now()
        kept = []
        for entry in self.read():
            if entry.get("stale"):
                continue
            expires = entry.get("expires")
            if expires and int(expires) <= now:
                continue
            kept.append(entry)
        return kept

    def blocked_terminal_ids(self) -> list[int]:
        found: set[int] = set()
        for entry in self.active():
            found.update(self.terminal_ids_for(entry))
        return sorted(found)

    def terminal_ids_for(self, entry: dict, commodity_only: bool = False) -> list[int]:
        scope = entry.get("scope")
        meta = _SCOPE.get(scope)
        if not meta:
            return []
        location_id = blacklist_id(entry.get("id"))
        if not isinstance(location_id, int):
            return []
        column = meta[1]
        sql = f"SELECT id FROM terminal WHERE {column} = ?"
        params: list = [location_id]
        if commodity_only:
            sql += " AND (type IS NULL OR type = 'commodity')"
        try:
            rows = self.helper.get_database().execute_fetchall(sql, tuple(params))
        except Exception:
            return []
        return [int(row["id"]) for row in rows]

    def terminal_count(self, entry: dict) -> int:
        return len(self.terminal_ids_for(entry, commodity_only=True))

    def needs_confirm(self, action: str, scope: str | None = None, terminal_count: int = 0, removing: int = 1) -> bool:
        if action == "clear":
            return True
        if action == "unblock":
            return removing > 1
        if scope in ("system", "planet"):
            return True
        return terminal_count > TERMINAL_CONFIRM_LIMIT

    def remember_preview(self, signature: str) -> None:
        _PREVIEWS[self.path] = {"signature": signature, "until": self.now() + CONFIRM_SECONDS}

    def accept_confirm(self, signature: str) -> bool:
        pending = _PREVIEWS.get(self.path)
        if not pending:
            return False
        if self.now() > int(pending["until"]):
            _PREVIEWS.pop(self.path, None)
            return False
        if pending["signature"] != signature:
            return False
        _PREVIEWS.pop(self.path, None)
        return True

    def add(self, candidate: dict, reason: str | None, expires: int | None) -> dict:
        entries = [entry for entry in self.read() if not _same_key(entry, candidate["scope"], candidate["id"])]
        entry = {
            "scope": candidate["scope"],
            "id": int(candidate["id"]),
            "name": candidate["name"],
            "path": candidate.get("path") or "",
            "reason": reason or "",
            "added": self.now(),
            "expires": expires,
            "stale": False,
        }
        entries.append(entry)
        self._write(entries)
        return entry

    def remove_keys(self, keys: list[tuple[str, int]]) -> int:
        wanted = set(keys)
        entries = self.read()
        kept = [entry for entry in entries if (entry.get("scope"), blacklist_id(entry.get("id"))) not in wanted]
        self._write(kept)
        return len(entries) - len(kept)

    def clear(self, only_expired: bool = False) -> int:
        entries = self.read()
        now = self.now()
        if only_expired:
            kept = [entry for entry in entries if not entry.get("expires") or int(entry["expires"]) > now]
        else:
            kept = []
        self._write(kept)
        return len(entries) - len(kept)

    def matching_blocks(self, name: str) -> list[dict]:
        text = (name or "").strip()
        if not text:
            return []
        entries = self.active()
        scoped = _parse_block_ref(text)
        if scoped:
            return [entry for entry in entries if entry.get("scope") == scoped[0] and blacklist_id(entry.get("id")) == scoped[1]]
        folded = text.casefold()
        by_name = [entry for entry in entries if str(entry.get("name") or "").casefold() == folded]
        if by_name:
            return by_name
        if text.isdigit():
            number = int(text)
            return [entry for entry in entries if blacklist_id(entry.get("id")) == number]
        return []

    def sync_after_import(self) -> None:
        """Refresh names after a good import. A failed or empty import does not touch the file."""
        if not getattr(self.helper, "is_ready", lambda: True)():
            return
        if self._import_failed() or self._terminal_count() == 0:
            return
        before = _file_bytes(self.path)
        self.migrate()
        entries = self._read_file()
        if not entries:
            # migrate() may have created the file. Don't rewrite an unchanged empty state.
            return
        refreshed = [self._refresh_entry(entry) for entry in entries]
        if _file_bytes(self.path) != before or refreshed != entries:
            self._write(refreshed)

    def migrate(self) -> None:
        """Copy blocked terminals once, then rename the old file so it is never read again."""
        if not os.path.exists(self._legacy):
            return
        try:
            with open(self._legacy, "r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
        except Exception as error:
            warn(f"UEX terminal blacklist was not migrated ({error}). The file was left in place.")
            return
        rows = loaded if isinstance(loaded, list) else []
        entries = self._read_file() if os.path.exists(self.path) else []
        existing = {(entry.get("scope"), blacklist_id(entry.get("id"))) for entry in entries}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not blacklist_flag(row.get("is_blacklisted")):
                continue
            terminal_id = blacklist_id(row.get("id"))
            if not isinstance(terminal_id, int):
                continue
            if ("terminal", terminal_id) in existing:
                continue
            name = row.get("terminal") or row.get("name") or str(terminal_id)
            path = ", ".join(part for part in (row.get("system"), row.get("orbit")) if part)
            entries.append(
                {
                    "scope": "terminal",
                    "id": terminal_id,
                    "name": name,
                    "path": path,
                    "reason": MIGRATED_REASON,
                    "added": self.now(),
                    "expires": None,
                    "stale": False,
                }
            )
            existing.add(("terminal", terminal_id))
        if entries and not self._write(entries):
            warn("UEX terminal blacklist was not migrated. The previous files were kept.")
            return
        retired = self._legacy + ".migrated"
        try:
            os.replace(self._legacy, retired)
        except OSError as error:
            warn(f"UEX terminal blacklist was migrated but could not be retired ({error}).")

    def _refresh_entry(self, entry: dict) -> dict:
        scope = entry.get("scope")
        meta = _SCOPE.get(scope)
        location_id = blacklist_id(entry.get("id"))
        updated = dict(entry)
        updated["id"] = location_id if isinstance(location_id, int) else entry.get("id")
        updated["stale"] = bool(blacklist_flag(entry.get("stale"))) if isinstance(entry.get("stale"), str) else bool(entry.get("stale"))
        if not meta or not isinstance(location_id, int):
            updated["stale"] = True
            return updated
        row = self._lookup(meta[0], location_id)
        if row:
            updated["name"] = row["name"]
            updated["path"] = _path_from_row(meta[3], row)
            updated["stale"] = False
            return updated
        matches = self._exact_name(meta[0], entry.get("name"))
        system = _system_of(entry)
        if system and meta[0] != "star_system":
            matches = [
                item
                for item in matches
                if str(item.get("star_system_name") or "").casefold() == system.casefold()
            ]
        if len(matches) == 1:
            updated["id"] = int(matches[0]["id"])
            updated["name"] = matches[0]["name"]
            updated["path"] = _path_from_row(meta[3], matches[0])
            updated["stale"] = False
            return updated
        updated["stale"] = True
        return updated

    def _lookup(self, table: str, location_id: int) -> dict | None:
        try:
            rows = self.helper.get_database().execute_fetchall(
                f"SELECT * FROM {table} WHERE id = ?",
                (location_id,),
            )
        except Exception:
            return None
        return dict(rows[0]) if rows else None

    def _exact_name(self, table: str, name: str | None) -> list[dict]:
        if not name:
            return []
        try:
            rows = self.helper.get_database().execute_fetchall(
                f"SELECT * FROM {table} WHERE name = ? COLLATE NOCASE",
                (name,),
            )
        except Exception:
            return []
        return [dict(row) for row in rows]

    def _import_failed(self) -> bool:
        handler = self.helper.get_handler_import() if hasattr(self.helper, "get_handler_import") else None
        return bool(handler is not None and getattr(handler, "import_failed", False))

    def _terminal_count(self) -> int:
        try:
            rows = self.helper.get_database().execute_fetchall("SELECT COUNT(*) AS n FROM terminal")
        except Exception:
            return 0
        return int(rows[0]["n"]) if rows else 0

    def _read_file(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
        except Exception as error:
            self._quarantine(error)
            return []
        if loaded in (None, []):
            return []
        if not isinstance(loaded, list):
            self._quarantine("file is not a list")
            return []
        cleaned = []
        for entry in loaded:
            if isinstance(entry, dict) and entry.get("scope") in _SCOPE:
                cleaned.append(_normalise_entry(entry))
        return cleaned

    def _quarantine(self, error) -> None:
        retired = f"{self.path}.corrupt-{self.now()}"
        try:
            os.replace(self.path, retired)
        except OSError:
            return
        warn(f"UEX location blocklist was unreadable ({error}). It was renamed to {os.path.basename(retired)} and was not overwritten.")

    def _write(self, entries: list[dict]) -> bool:
        now = self.now()
        kept = []
        for entry in entries:
            expires = entry.get("expires")
            if expires and int(expires) <= now:
                continue
            kept.append(_normalise_entry(entry))
        kept.sort(key=lambda entry: (str(entry.get("scope")), int(entry.get("id") or 0)))
        payload = (
            "# Location blocklist. Keyed by scope and UEX id.\n"
            "# To unblock, remove the entry with uex_unblock_location or set expires in the past.\n"
            "# Do not delete a line to mean 'not blocked' without saving the file; the tools write this file.\n"
            "# reason is optional. expires is a unix time, or omit it for a block that does not expire.\n"
            "# A quoted false expiry means the block does not expire.\n\n"
            + yaml.safe_dump(kept, sort_keys=False, allow_unicode=True)
        )
        temporary = self.path + ".tmp"
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temporary, self.path)
        except OSError as error:
            warn(f"UEX location blocklist was not rewritten ({error}). The previous file was kept.")
            try:
                os.remove(temporary)
            except OSError:
                pass
            return False
        return True


def signature(action: str, fields: dict) -> str:
    body = {"action": action}
    body.update(fields)
    return json.dumps(body, sort_keys=True, default=str)


def block_reason_for_terminal(helper, terminal) -> str | None:
    try:
        if helper is None or not hasattr(helper, "get_data_path"):
            return None
        store = LocationBlocklist(helper)
        terminal_id = terminal.get_id()
        for entry in store.active():
            if terminal_id in store.terminal_ids_for(entry):
                reason = entry.get("reason") or "blocked"
                return str(reason)
    except Exception:
        return None
    return None


def _normalise_entry(entry: dict) -> dict:
    expires = _falsey_expiry(entry.get("expires"))
    stale = entry.get("stale")
    if isinstance(stale, str):
        stale = blacklist_flag(stale)
    return {
        "scope": entry.get("scope"),
        "id": blacklist_id(entry.get("id")),
        "name": entry.get("name") or "",
        "path": entry.get("path") or "",
        "reason": entry.get("reason") or "",
        "added": entry.get("added"),
        "expires": expires,
        "stale": bool(stale),
    }


def _same_key(entry: dict, scope: str, location_id: int) -> bool:
    return entry.get("scope") == scope and blacklist_id(entry.get("id")) == int(location_id)


def _parse_block_ref(text: str) -> tuple[str, int] | None:
    if ":" not in text:
        return None
    scope, _, raw_id = text.partition(":")
    scope = scope.strip().lower()
    if scope not in _SCOPE:
        return None
    location_id = blacklist_id(raw_id.strip())
    if not isinstance(location_id, int):
        return None
    return scope, location_id


def _path_from_row(columns: tuple, row: dict) -> str:
    parts = []
    for column in columns:
        value = row.get(column)
        if value and value not in parts:
            parts.append(str(value))
    return ", ".join(parts)


def _system_of(entry: dict) -> str:
    path = str(entry.get("path") or "")
    if not path:
        return ""
    return path.split(",")[0].strip()


def _file_bytes(path: str) -> bytes | None:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        return handle.read()


def format_expiry(expires) -> str:
    if not expires:
        return ""
    moment = datetime.fromtimestamp(int(expires), tz=timezone.utc)
    return moment.strftime("%d %b").lstrip("0")
