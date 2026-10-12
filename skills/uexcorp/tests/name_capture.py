"""Frozen name-resolution capture for the steps 1-6 refactor.

System One is off unless a row says otherwise. The network guard counts
real socket connects and must stay at 0. Flags are derived from the note
and the query so a later note rewrite still has a structured pin.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

from skills.uexcorp.uexcorp.api.llm import _CODE_STOPLIST
from skills.uexcorp.uexcorp.naming.flags import flags_for
from skills.uexcorp.tests.test_name_ranker import CASES, CORPORA, _offline_llm, run_case
from skills.uexcorp.tests.test_read_place_typos import _FIXTURE as _PLACE_FIXTURE
from skills.uexcorp.tests.test_system_one_accepts import _TYPOS

_CAPTURE_PATH = Path(__file__).with_name("name_capture.json")
_WORD = re.compile(r"[A-Za-z0-9']+")


def _place_rows() -> list[dict]:
    rows = []
    for item in _PLACE_FIXTURE["places"]:
        name = item["n"]
        scope = item["s"]
        row_id = item["i"]
        aliases = [str(item["a"]).casefold()] if item.get("a") else []
        rows.append({
            "scope": scope,
            "id": row_id,
            "name": name,
            "key": f"{scope}:{row_id}",
            "label": name,
            "aliases": aliases,
            "parents": {},
            "path": "",
            "system_name": "",
            "system_id": None,
        })
    return rows


def _row(query: str, match, note, calls: int, method=None, candidates=None) -> list:
    return [
        query,
        match,
        note,
        calls,
        method,
        candidates or [],
        flags_for(note, query),
    ]


async def _plain(llm, calls: dict, query: str, names: list[str]) -> list:
    before = calls["decide"]
    match, note = await llm.find_closest_match(query, names)
    found = await llm.find_closest_key(query, {name: name for name in names})
    candidates = [item.get("label") or item.get("name") for item in found.candidates]
    return _row(query, match, note, calls["decide"] - before, found.method, candidates)


async def _place(llm, calls: dict, query: str, rows: list[dict]) -> list:
    before = calls["decide"]
    options = {row["key"]: row["name"] for row in rows}
    labels = {row["key"]: row["label"] for row in rows}
    found = await llm.find_closest_key(query, options, labels, records=rows)
    match = None
    if found.key:
        match = options.get(found.key)
    candidates = [f"{item.get('key')}|{item.get('name')}" for item in found.candidates]
    return _row(query, match, found.note, calls["decide"] - before, found.method, candidates)


def _block_roundtrip() -> dict:
    """Unblock by name and by scope:id, plus the legacy id copy. Dates omitted."""
    import yaml

    from skills.uexcorp.uexcorp.handler.config_handler import blacklist_flag, blacklist_id
    from skills.uexcorp.uexcorp.location_blocklist import LocationBlocklist

    ids = [blacklist_id(value) for value in (52, "52", "nope", True, None)]
    with tempfile.TemporaryDirectory() as directory:
        helper = type("H", (), {
            "get_data_path": lambda self: directory,
            "get_timestamp": lambda self: 1_700_000_000,
        })()
        from skills.uexcorp.uexcorp.location_blocklist import config_dir

        legacy = os.path.join(config_dir(helper), "terminal_blacklist.yaml")
        with open(legacy, "w", encoding="utf-8") as handle:
            yaml.safe_dump(
                [
                    {"id": "7", "terminal": "Levski", "is_blacklisted": True, "system": "Nyx"},
                    {"id": 8, "terminal": "Area18", "is_blacklisted": False},
                ],
                handle,
            )
        blocklist = LocationBlocklist(helper)
        blocklist.migrate()
        stored = [
            {"scope": entry.get("scope"), "id": entry.get("id"), "name": entry.get("name")}
            for entry in blocklist.read()
        ]
        by_name = [
            {"scope": entry.get("scope"), "id": entry.get("id"), "name": entry.get("name")}
            for entry in blocklist.matching_blocks("Levski")
        ]
        by_ref = [
            {"scope": entry.get("scope"), "id": entry.get("id"), "name": entry.get("name")}
            for entry in blocklist.matching_blocks("terminal:7")
        ]
        unknown = blocklist.matching_blocks("unknown:5")
        commodity_path = os.path.join(config_dir(helper), "commodity_blacklist.yaml")
        with open(commodity_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(
                [
                    {"id": "44", "name": "Iron", "is_blacklisted": True},
                    {"id": 33, "name": "Gold", "is_blacklisted": "false"},
                ],
                handle,
            )
        with open(commodity_path, "r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        commodity_file = [
            {
                "id": blacklist_id(entry.get("id")),
                "name": entry.get("name"),
                "is_blacklisted": blacklist_flag(entry.get("is_blacklisted")),
            }
            for entry in loaded
        ]
    return {
        "blacklist_ids": ids,
        "commodity_file": commodity_file,
        "migrated": stored,
        "by_name": by_name,
        "by_ref": by_ref,
        "unknown_ref": unknown,
    }


async def _build_async() -> dict:
    llm, calls = _offline_llm()
    corpus = json.loads(Path(__file__).with_name("name_corpus.json").read_text(encoding="utf-8"))
    sections: dict[str, list] = {}

    baseline = []
    for case in CASES:
        snap = await run_case(case)
        note = None
        if case["kind"] == "name":
            _match, note = await llm.find_closest_match(case["query"], list(CORPORA[case["corpus"]]))
        baseline.append([
            case["id"],
            snap["match"],
            snap["picked"],
            snap["method"],
            snap["candidates"],
            note,
            snap["decide"],
            flags_for(note, case["query"]),
        ])
    sections["baseline"] = baseline

    commodities = list(corpus["commodities"])
    sections["commodities"] = [await _plain(llm, calls, name, commodities) for name in commodities]
    sections["commodity_codes"] = [
        await _plain(llm, calls, query, commodities) for query in corpus["commodity_codes"]
    ]
    vehicles = list(corpus["vehicles"])
    sections["vehicles"] = [await _plain(llm, calls, name, vehicles) for name in vehicles]
    sections["vehicle_nicknames"] = [
        await _plain(llm, calls, query, vehicles) for query in corpus["vehicle_nicknames"]
    ]
    items = list(corpus["items"])
    sections["items"] = [await _plain(llm, calls, name, items) for name in items]
    categories = list(corpus["item_category_names"])
    sections["categories"] = [await _plain(llm, calls, name, categories) for name in categories]
    sections["slugs"] = [
        await _plain(llm, calls, query, categories) for query in corpus["item_slugs"]
    ]

    def _folded(names: list[str]) -> list[str]:
        extra = []
        seen_names = set(names)
        for name in names:
            folded = name.casefold()
            if folded not in seen_names and folded not in extra:
                extra.append(folded)
        return extra

    sections["commodities_casefold"] = [
        await _plain(llm, calls, query, commodities) for query in _folded(commodities)
    ]
    sections["vehicles_casefold"] = [
        await _plain(llm, calls, query, vehicles) for query in _folded(vehicles)
    ]
    sections["items_casefold"] = [
        await _plain(llm, calls, query, items) for query in _folded(items)
    ]
    sections["categories_casefold"] = [
        await _plain(llm, calls, query, categories) for query in _folded(categories)
    ]

    stoplist = []
    for code in sorted(_CODE_STOPLIST):
        for query in (code.upper(), code[:1].upper() + code[1:], code):
            stoplist.append(await _plain(llm, calls, query, commodities))
    sections["stoplist"] = stoplist

    rows = _place_rows()
    place_names = []
    seen = set()
    for row in rows:
        if row["name"] in seen:
            continue
        seen.add(row["name"])
        place_names.append(await _place(llm, calls, row["name"], rows))
    sections["place_names"] = place_names
    sections["place_names_casefold"] = [
        await _place(llm, calls, query, rows) for query in _folded([row["name"] for row in rows])
    ]

    words = sorted({word for row in rows for word in _WORD.findall(row["name"])})
    sections["whole_words"] = [await _place(llm, calls, word, rows) for word in words]

    typos = []
    for case in _PLACE_FIXTURE["cases"]:
        typos.append(await _place(llm, calls, case["query"], rows))
    sections["place_typos_offline"] = typos
    sections["place_typos_wrong_shortlist"] = [
        await _wrong_shortlist(case["query"], case["target"], rows)
        for case in _PLACE_FIXTURE["cases"]
    ]

    plain_typos = []
    for query, _target, kind in _TYPOS:
        names = list(corpus[kind])
        plain_typos.append(await _plain(llm, calls, query, names))
    sections["plain_typos_offline"] = plain_typos

    count = sum(len(value) for value in sections.values())
    return {
        "queries": count,
        "whole_words": len(words),
        "place_rows": len(rows),
        "network": 0,
        "blocks": _block_roundtrip(),
        "sections": sections,
    }


async def _wrong_shortlist(query: str, target: str, rows: list[dict]) -> list:
    """System One returns a different name that is still on the shortlist."""
    from types import SimpleNamespace

    from skills.uexcorp.uexcorp.api.llm import Llm

    calls = {"n": 0}

    class _Stub:
        available = True

        def choice(self, prompt, criteria=None, examples=None, **kwargs):
            self.offered = [name for name in (criteria or {}) if name != "none_of_these"]
            return {"criteria": criteria or {}}

        def noul(self, *args, **kwargs):
            return {}

        async def decide(self, state=None, questions=None):
            calls["n"] += 1
            offered = list(self.offered)
            wrong = next((name for name in offered if name != target), None)

            class _Answers:
                seconds = 0.0

                def choice(self, key, min_confidence=0.0):
                    return wrong

                def confidence(self, key):
                    return 0.99

            return _Answers()

    stub = _Stub()
    helper = SimpleNamespace(
        get_handler_debug=lambda: SimpleNamespace(write=lambda *args, **kwargs: None),
        get_handler_error=lambda: SimpleNamespace(write=lambda *args, **kwargs: None),
        add_context=lambda *args, **kwargs: None,
        get_handler_config=lambda: SimpleNamespace(
            get_wingman=lambda: SimpleNamespace(system_one=stub, ai=SimpleNamespace())
        ),
    )
    ranker = Llm(helper)

    async def _no_model(*args, **kwargs):
        return None

    ranker.call = _no_model
    options = {row["key"]: row["name"] for row in rows}
    labels = {row["key"]: row["label"] for row in rows}
    found = await ranker.find_closest_key(query, options, labels, records=rows)
    match = options.get(found.key) if found.key else None
    candidates = [f"{item.get('key')}|{item.get('name')}" for item in found.candidates]
    return _row(query, match, found.note, calls["n"], found.method, candidates)


def build_capture() -> dict:
    return asyncio.run(_build_async())


def load_capture() -> dict:
    return json.loads(_CAPTURE_PATH.read_text(encoding="utf-8"))
