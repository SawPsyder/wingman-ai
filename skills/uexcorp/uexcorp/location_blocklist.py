"""Persistent location blocklist.

The yaml file is the only record. Imports must not drop or rewrite it when
the import failed or the location tables are empty. Expired rows stop
applying immediately and are removed on the next write.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, datetime, timezone

import yaml

from skills.uexcorp.uexcorp.handler.config_handler import blacklist_flag, blacklist_id
from skills.uexcorp.uexcorp.log import warn

CONFIRM_SECONDS = 120
TERMINAL_CONFIRM_LIMIT = 10
MIGRATED_REASON = "migrated"
MIGRATED_MARKER = "migrated_terminal_blacklist:"

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
_LOCK = threading.RLock()
_LEGACY_WARNED: set[str] = set()


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


def _expiry_value(value):
    """Unix UTC, or None when the block does not expire. Never raises."""
    if isinstance(value, str) and not blacklist_flag(value):
        return None
    if value in (None, False) or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp())
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _falsey_expiry(value):
    return _expiry_value(value)


def _id_token(value):
    parsed = blacklist_id(value)
    if isinstance(parsed, bool):
        return parsed
    if isinstance(parsed, int):
        return parsed
    if value is None:
        return None
    return str(value).strip()


def _ids_equal(left, right) -> bool:
    return _id_token(left) == _id_token(right)


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
            if entry.get("stale") or entry.get("invalid"):
                continue
            expires = _expiry_value(entry.get("expires"))
            if expires and expires <= now:
                continue
            kept.append(entry)
        return kept

    def blocked_terminal_ids(self) -> list[int]:
        found: set[int] = set()
        for entry in cached_active_entries(self.helper):
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

    def terminal_total(self, entry: dict) -> int:
        return len(self.terminal_ids_for(entry, commodity_only=False))

    def lagrange_sentence(self, candidate: dict) -> str:
        """Say whether this planet block's cascade covers its Lagrange stations."""
        if candidate.get("scope") != "planet":
            return ""
        planet_id = blacklist_id(candidate.get("id"))
        if not isinstance(planet_id, int):
            return ""
        try:
            stations = self.helper.get_database().execute_fetchall(
                "SELECT id, name FROM space_station WHERE id_planet = ? AND is_lagrange = 1",
                (planet_id,),
            )
        except Exception:
            return ""
        covered = set(self.terminal_ids_for(candidate))
        included = []
        excluded = []
        for station in stations:
            try:
                terminals = self.helper.get_database().execute_fetchall(
                    "SELECT id FROM terminal WHERE id_space_station = ?",
                    (int(station["id"]),),
                )
            except Exception:
                continue
            ids = [int(row["id"]) for row in terminals]
            if not ids:
                continue
            name = str(station["name"] or station["id"])
            if all(terminal_id in covered for terminal_id in ids):
                included.append(name)
            elif any(terminal_id in covered for terminal_id in ids):
                included.append(name)
                excluded.append(name)
            else:
                excluded.append(name)
        parts = []
        if included:
            parts.append("Includes Lagrange stations: " + ", ".join(included) + ".")
        if excluded:
            parts.append("Excludes Lagrange stations: " + ", ".join(excluded) + ".")
        return " ".join(parts)

    def needs_confirm(self, action: str, scope: str | None = None, terminal_count: int = 0, removing: int = 1) -> bool:
        if action == "clear":
            return True
        if action == "unblock":
            return removing > 1
        if scope in ("system", "planet"):
            return True
        return terminal_count > TERMINAL_CONFIRM_LIMIT

    def remember_preview(self, signature: str, choice: str | None = None) -> None:
        """Hold a preview for ``CONFIRM_SECONDS``.

        ``choice`` is the resolved scope:id. A confirm with the same signature
        reuses it, so System One is not asked to pick the name again. The noul
        yes-check on confirm is a separate call and still runs.
        """
        _PREVIEWS[self.path] = {
            "signature": signature,
            "until": self.now() + CONFIRM_SECONDS,
            "choice": choice,
        }

    def pending_choice(self, signature: str) -> str | None:
        """The preview's scope:id while that signature is inside the window."""
        pending = _PREVIEWS.get(self.path)
        if not pending:
            return None
        if self.now() > int(pending["until"]):
            return None
        if pending["signature"] != signature:
            return None
        choice = pending.get("choice")
        if not choice:
            return None
        return str(choice)

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
        with _LOCK:
            entries = [
                entry
                for entry in self.read()
                if not _same_key(entry, candidate["scope"], candidate["id"])
            ]
            location_id = blacklist_id(candidate["id"])
            entry = {
                "scope": str(candidate["scope"]).strip().lower(),
                "id": location_id if isinstance(location_id, int) else candidate["id"],
                "name": candidate["name"],
                "path": candidate.get("path") or "",
                "reason": reason or "",
                "added": self.now(),
                "expires": expires,
                "stale": False,
            }
            entries.append(entry)
            if not self._write(entries):
                return None
            return _normalise_entry(entry)

    def remove_keys(self, keys: list[tuple]) -> int:
        with _LOCK:
            wanted = {(str(scope).strip().lower(), _id_token(location_id)) for scope, location_id in keys}
            entries = self.read()
            kept = [
                entry
                for entry in entries
                if (entry.get("scope"), _id_token(entry.get("id"))) not in wanted
            ]
            self._write(kept)
            return len(entries) - len(kept)

    def clear(self, only_expired: bool = False) -> int:
        with _LOCK:
            entries = self.read()
            now = self.now()
            if only_expired:
                kept = []
                for entry in entries:
                    expires = _expiry_value(entry.get("expires"))
                    if not expires or expires > now:
                        kept.append(entry)
            else:
                kept = []
            self._write(kept)
            return len(entries) - len(kept)

    def matching_blocks(self, name: str) -> list[dict]:
        """Active, stale, and invalid rows. Expired rows still on disk count too."""
        text = (name or "").strip()
        if not text:
            return []
        entries = self.read()
        scoped = _parse_block_ref(text)
        if scoped:
            return [
                entry
                for entry in entries
                if entry.get("scope") == scoped[0] and _ids_equal(entry.get("id"), scoped[1])
            ]
        folded = text.casefold()
        by_name = [entry for entry in entries if str(entry.get("name") or "").casefold() == folded]
        if by_name:
            return by_name
        if text.isdigit():
            number = int(text)
            return [entry for entry in entries if _ids_equal(entry.get("id"), number)]
        return []

    def sync_after_import(self) -> None:
        """Refresh names after a good import. A failed or empty import does not touch the file."""
        with _LOCK:
            if not getattr(self.helper, "is_ready", lambda: True)():
                return
            if self._import_failed() or self._terminal_count() == 0:
                return
            before = _file_bytes(self.path)
            self._migrate_body()
            entries = self._read_file()
            if not entries:
                # migrate() may have created the file. Don't rewrite an unchanged empty state.
                return
            refreshed = [self._refresh_entry(entry) for entry in entries]
            if _file_bytes(self.path) != before or refreshed != entries:
                self._write(refreshed)

    def migrate(self) -> None:
        """Copy blocked terminals once, then rename the old file so it is never read again."""
        with _LOCK:
            self._migrate_body()

    def _migrate_body(self) -> None:
        if self._has_marker():
            if not os.path.exists(self._legacy):
                return
            # Retire failed earlier: the original file is still here and
            # .migrated does not. Retry the rename only, so an unblock is
            # not undone by copying the same rows again.
            if not os.path.exists(self._legacy + ".migrated"):
                self._retire_legacy()
                return
            added = self._copy_legacy_rows()
            if added is None:
                return
            if added:
                warn(
                    "UEX terminal blacklist had newer blocked terminals after migration. "
                    f"{added} were merged. The previous .migrated file was left in place."
                )
            else:
                warn(
                    "UEX terminal blacklist appeared again after migration. "
                    "No new rows were merged. The previous .migrated file was left in place."
                )
            self._retire_legacy()
            return
        if not os.path.exists(self._legacy):
            return
        added = self._copy_legacy_rows()
        if added is None:
            return
        if not self._has_marker():
            current = self._read_file() if os.path.exists(self.path) else []
            if not self._write(current, migrated=True):
                warn("UEX terminal blacklist was not migrated. The previous files were kept.")
                return
        self._retire_legacy()

    def _copy_legacy_rows(self) -> int | None:
        """Append blacklisted legacy rows that are not already stored.

        Returns how many were added, or None when the file could not be read.
        """
        try:
            with open(self._legacy, "r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
        except Exception as error:
            if self._legacy not in _LEGACY_WARNED:
                _LEGACY_WARNED.add(self._legacy)
                warn(f"UEX terminal blacklist was not migrated ({error}). The file was left in place.")
            return None
        rows = loaded if isinstance(loaded, list) else []
        entries = self._read_file() if os.path.exists(self.path) else []
        existing = {(entry.get("scope"), _id_token(entry.get("id"))) for entry in entries}
        added = 0
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
            added += 1
        if added and not self._write(entries, migrated=True):
            warn("UEX terminal blacklist was not migrated. The previous files were kept.")
            return None
        return added

    def _retire_legacy(self) -> None:
        if not os.path.exists(self._legacy):
            return
        retired = self._legacy + ".migrated"
        if os.path.exists(retired):
            retired = self._unique_retired_name()
        try:
            os.replace(self._legacy, retired)
        except OSError as error:
            warn(f"UEX terminal blacklist was migrated but could not be retired ({error}).")

    def _unique_retired_name(self) -> str:
        stamp = self.now()
        candidate = f"{self._legacy}.migrated-{stamp}"
        counter = 0
        while os.path.exists(candidate):
            counter += 1
            candidate = f"{self._legacy}.migrated-{stamp}-{counter}"
        return candidate

    def _has_marker(self) -> bool:
        if not os.path.exists(self.path):
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return MIGRATED_MARKER in handle.read(4096)
        except OSError:
            return False

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
            if isinstance(entry, dict):
                cleaned.append(_normalise_entry(entry))
        return cleaned

    def _quarantine(self, error) -> None:
        retired = f"{self.path}.corrupt-{self.now()}"
        try:
            os.replace(self.path, retired)
        except OSError:
            return
        warn(f"UEX location blocklist was unreadable ({error}). It was renamed to {os.path.basename(retired)} and was not overwritten.")

    def _write(self, entries: list[dict], migrated: bool | None = None) -> bool:
        with _LOCK:
            return self._write_body(entries, migrated)

    def _write_body(self, entries: list[dict], migrated: bool | None) -> bool:
        now = self.now()
        if migrated is None:
            migrated = self._has_marker()
        kept = []
        for entry in entries:
            expires = _expiry_value(entry.get("expires"))
            if expires and expires <= now:
                continue
            kept.append(_normalise_entry(entry))
        kept.sort(key=lambda entry: (str(entry.get("scope") or ""), _sort_id(entry.get("id"))))
        header = (
            "# Location blocklist. Keyed by scope and UEX id.\n"
            "# To unblock, remove the entry with uex_unblock_location or set expires in the past.\n"
            "# Do not delete a line to mean 'not blocked' without saving the file; the tools write this file.\n"
            "# reason is optional. expires is a unix time, or omit it for a block that does not expire.\n"
            "# A quoted false expiry means the block does not expire.\n"
        )
        if migrated:
            header += f"# {MIGRATED_MARKER} true\n"
        payload = header + "\n" + yaml.safe_dump(kept, sort_keys=False, allow_unicode=True)
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
        try:
            self.helper._uex_block_snapshot = None
        except Exception:
            pass
        return True


def signature(action: str, fields: dict) -> str:
    body = {"action": action}
    body.update(fields)
    return json.dumps(body, sort_keys=True, default=str)


def _entry_is_active(entry: dict, now: int) -> bool:
    if entry.get("stale") or entry.get("invalid"):
        return False
    expires = _expiry_value(entry.get("expires"))
    if expires and expires <= now:
        return False
    return True


def cached_active_entries(helper) -> list[dict]:
    """Parsed rows cached by file mtime. Expiry is applied on every read.

    Terminal ids are expanded by the caller, not stored here, so a new
    terminal under a blocked parent is covered without a rewrite.
    """
    try:
        if helper is None or not hasattr(helper, "get_data_path"):
            return []
        store = LocationBlocklist(helper)
        path = store.path
        cached = getattr(helper, "_uex_block_snapshot", None)
        try:
            mtime = os.path.getmtime(path) if os.path.exists(path) else None
        except OSError:
            mtime = None
        rows = None
        if isinstance(cached, dict) and cached.get("path") == path and cached.get("mtime") == mtime:
            rows = cached.get("entries")
        if not isinstance(rows, list):
            rows = store.read()
            try:
                mtime = os.path.getmtime(path) if os.path.exists(path) else None
            except OSError:
                mtime = None
            try:
                helper._uex_block_snapshot = {"path": path, "mtime": mtime, "entries": rows}
            except Exception:
                pass
        now = int(helper.get_timestamp())
        return [entry for entry in rows if _entry_is_active(entry, now)]
    except Exception:
        return []


def block_reason_for_terminal(helper, terminal) -> str | None:
    try:
        if helper is None or not hasattr(helper, "get_data_path"):
            return None
        store = LocationBlocklist(helper)
        terminal_id = terminal.get_id()
        for entry in cached_active_entries(helper):
            if terminal_id in store.terminal_ids_for(entry):
                return str(entry.get("reason") or "blocked")
    except Exception:
        return None
    return None


def _normalise_entry(entry: dict) -> dict:
    scope = str(entry.get("scope") or "").strip().lower()
    raw_id = entry.get("id")
    location_id = blacklist_id(raw_id)
    if isinstance(location_id, bool) or not isinstance(location_id, int):
        stored_id = raw_id
        invalid = True
    else:
        stored_id = location_id
        invalid = scope not in _SCOPE
    expires = _expiry_value(entry.get("expires"))
    stale = entry.get("stale")
    if isinstance(stale, str):
        stale = blacklist_flag(stale)
    if invalid:
        stale = True
    return {
        "scope": scope,
        "id": stored_id,
        "name": entry.get("name") or "",
        "path": entry.get("path") or "",
        "reason": entry.get("reason") or "",
        "added": entry.get("added"),
        "expires": expires,
        "stale": bool(stale),
        "invalid": bool(invalid),
    }


def _same_key(entry: dict, scope: str, location_id) -> bool:
    return entry.get("scope") == str(scope).strip().lower() and _ids_equal(entry.get("id"), location_id)


def _parse_block_ref(text: str) -> tuple | None:
    if ":" not in text:
        return None
    scope, _, raw_id = text.partition(":")
    scope = scope.strip().lower()
    if scope not in _SCOPE:
        return None
    token = raw_id.strip()
    if not token:
        return None
    return scope, _id_token(token)


def _sort_id(value):
    token = _id_token(value)
    if isinstance(token, int) and not isinstance(token, bool):
        return (0, token, "")
    return (1, 0, str(token))


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


def format_expiry(expires, now=None) -> str:
    """Date, plus the local time and zone when less than two days are left."""
    stamp = _expiry_value(expires)
    if not stamp:
        return ""
    moment = datetime.fromtimestamp(stamp).astimezone()
    if now is not None and stamp - int(now) < 2 * 86400:
        zone = moment.tzname() or ""
        clock = moment.strftime("%d %b %H:%M").lstrip("0")
        return f"{clock} {zone}".strip()
    return moment.strftime("%d %b").lstrip("0")


def coverage_phrase(name: str, commodity: int, total: int) -> str:
    commodity_word = "commodity terminal" if commodity == 1 else "commodity terminals"
    total_word = "terminal" if total == 1 else "terminals"
    return f"{name}: {commodity} {commodity_word}, {total} {total_word} total"
