"""Golden name cases. Expected values are pinned from the frozen ranker.

Pools that need the live catalog use the same small commodity table as the
code tests. Place queries use the frozen place list.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

from skills.uexcorp.uexcorp.api.llm import _CODE_STOPLIST
from skills.uexcorp.tests.name_capture import _place_rows, _wrong_shortlist
from skills.uexcorp.tests.test_name_ranker import _offline_llm

_PATH = Path(__file__).with_name("golden_names.yaml")

# The code-test catalog: names, codes, and which holder is tradable.
_CATALOG = (
    ("Gold", "GOLD", True),
    ("Gold (Ore)", "GOLD", False),
    ("Iron", "IRON", True),
    ("Iron (Ore)", "IRONO", False),
    ("Laranite", "LARA", True),
    ("Laranite (Raw)", "LARA", False),
    ("Ammonia", "AMMO", True),
    ("Lycara", "LYCA", True),
    ("Freeze", "FREE", True),
    ("Caranite", "CARA", True),
    ("Caranite (Raw)", "CARW", False),
    ("Tritium", "TRIT", True),
    ("Torite", "TORI", True),
    ("Potassium", "POTA", True),
    ("Prota", "PROT", True),
    ("Pressurized Ice", "PICE", True),
    ("Ice (Raw)", "ICEW", False),
    ("Aluminum", "ALUM", True),
    ("Thermalfoam", "FOAM", True),
    ("Argon", "ARGO", True),
    ("Quantainium", "QUAN", True),
    ("Quantainium (Raw)", "QUAN", False),
    ("Hadinite", "HADA", True),
)

_GENERIC = ("mine", "star", "station", "outpost", "port", "gateway", "city", "depot", "hub")


def _records(tradable_only: bool = False) -> list[dict]:
    records = []
    for name, code, tradable in _CATALOG:
        if tradable_only and not tradable:
            continue
        record = {
            "key": name,
            "name": name,
            "aliases": [],
            "codes": [code],
            "is_raw": "(Ore)" in name or "(Raw)" in name,
        }
        # The reviewed alias file targets commodity:47, this catalog's Laranite.
        if name == "Laranite":
            record["id"] = 47
            record["kind"] = "commodity"
        records.append(record)
    from skills.uexcorp.uexcorp.naming.aliases import add_commodity_base_aliases

    add_commodity_base_aliases(records)
    return records


def _cases() -> list[dict]:
    cases = []
    for code in ("LARA", "Lara", "lara", "CARA", "Cara", "cara", "TRIT", "Trit", "trit",
                 "POTA", "Pota", "pota", "IRONO", "Irono", "irono"):
        cases.append({"id": f"code-{code}", "kind": "commodity", "pool": "catalog", "query": code})
    for code in sorted(_CODE_STOPLIST):
        for query in (code.upper(), code[:1].upper() + code[1:], code):
            cases.append({"id": f"stop-{query}", "kind": "commodity", "pool": "catalog", "query": query})
    for query in ("Ice", "Ice (Raw)", "Pressurized Ice", "Hadinite", "Quantanium", "Quantainium", "Laranit", "GOLD", "Gold", "gold", "Gold (Ore)"):
        cases.append({"id": f"name-{query}", "kind": "commodity", "pool": "catalog", "query": query})
    corpus = json.loads(Path(__file__).with_name("name_corpus.json").read_text(encoding="utf-8"))
    for query in ("Freelancer Max", "Mole", "Hornet", "Carrack"):
        cases.append({"id": f"vehicle-{query}", "kind": "vehicle", "pool": "vehicles", "query": query, "names": corpus["vehicles"]})
    cases.append({
        "id": "vehicle-Mercury",
        "kind": "vehicle",
        "pool": "vehicles",
        "query": "Mercury",
        "names": corpus["vehicles"],
        "nicknames": corpus["vehicle_nicknames"],
    })
    for query in ("Ruin", "Starlight", "Star", "Levski station", "Daymar Station", "Grim HEX", "GrimHEX",
                  "grim hex", "Seraphim", "Checkmate", "Baijini", "Jumptown", "Olisar", "Deal", "Babbage",
                  "Mine", "Fuel"):
        cases.append({"id": f"place-{query}", "kind": "place", "pool": "places", "query": query})
    for word in _GENERIC:
        cases.append({"id": f"generic-{word}", "kind": "place", "pool": "places", "query": word})
    return cases


async def _run_case(case: dict, llm, rows: list[dict]) -> dict:
    kind = case["kind"]
    query = case["query"]
    if kind == "commodity":
        records = _records(tradable_only=case["pool"] == "tradable")
        options = {row["name"]: row["name"] for row in records}
        found = await llm.find_closest_key(query, options, records=records)
        names = [item.get("label") or item.get("name") for item in found.candidates]
        return {"pick": found.key, "method": found.method, "candidates": names, "note": found.note}
    if kind == "vehicle":
        names = list(case["names"])
        if case.get("nicknames"):
            from skills.uexcorp.uexcorp.tool.validator import _vehicle_records

            records = _vehicle_records(_corpus_vehicles(names, case["nicknames"]))
            options = {row["name"]: row["name"] for row in records}
            found = await llm.find_closest_key(query, options, records=records)
            return {
                "pick": found.key,
                "method": found.method,
                "candidates": [item.get("label") for item in found.candidates],
                "note": found.note,
            }
        match, note = await llm.find_closest_match(query, names)
        found = await llm.find_closest_key(query, {name: name for name in names})
        return {
            "pick": match,
            "method": found.method,
            "candidates": [item.get("label") for item in found.candidates],
            "note": note,
        }
    found = await llm.find_closest_key(
        query,
        {row["key"]: row["name"] for row in rows},
        {row["key"]: row["label"] for row in rows},
        records=rows,
    )
    return {
        "pick": ({row["key"]: row["name"] for row in rows}).get(found.key) if found.key else None,
        "method": found.method,
        "candidates": [item.get("name") for item in found.candidates],
        "note": found.note,
    }


async def _all() -> list[dict]:
    llm, _calls = _offline_llm()
    rows = _place_rows()
    built = []
    for case in _cases():
        result = await _run_case(case, llm, rows)
        stored = {key: case[key] for key in ("id", "kind", "pool", "query")}
        stored["expect"] = result
        built.append(stored)
    wrong = await _wrong_shortlist("Lorvil", "Lorville", rows)
    built.append({
        "id": "adversarial-lorvil",
        "kind": "place",
        "pool": "places",
        "query": "Lorvil",
        "expect": {
            "pick": wrong[1],
            "method": wrong[4],
            "candidates": wrong[5],
            "note": wrong[2],
            "model_calls": wrong[3],
        },
    })
    return built


def _slug_from_short(short: str) -> str:
    """Hyphenated short name, the slug shape ``mercury-star-runner`` uses."""
    chars = []
    hyphen = False
    for char in short.casefold():
        if char.isalnum():
            chars.append(char)
            hyphen = False
        elif not hyphen:
            chars.append("-")
            hyphen = True
    return "".join(chars).strip("-")


class _CorpusVehicle:
    def __init__(self, short: str, full: str):
        self._short = short
        self._full = full

    def __str__(self) -> str:
        return self._full

    def get_name(self) -> str:
        return self._short

    def get_slug(self) -> str | None:
        # The short name is the model. The company lives only on the full name,
        # so a missing short name does not invent a company slug.
        if self._short.casefold() == self._full.casefold():
            return None
        return _slug_from_short(self._short)


def _corpus_vehicles(names: list[str], nicknames: dict) -> list[_CorpusVehicle]:
    """One vehicle per full name. A null nickname matches one unclaimed name."""
    claimed = {}
    for short, full in nicknames.items():
        if full:
            claimed.setdefault(full, short)
    vehicles = []
    used = set()
    for full in names:
        if full in claimed:
            vehicles.append(_CorpusVehicle(claimed[full], full))
            used.add(full)
    for short, full in nicknames.items():
        if full:
            continue
        hits = [name for name in names if name not in used and (name == short or name.endswith(" " + short))]
        if len(hits) == 1:
            vehicles.append(_CorpusVehicle(short, hits[0]))
            used.add(hits[0])
    for full in names:
        if full not in used:
            vehicles.append(_CorpusVehicle(full, full))
    return vehicles


def load_golden() -> list[dict]:
    return yaml.safe_load(_PATH.read_text(encoding="utf-8"))


def build_golden() -> list[dict]:
    return asyncio.run(_all())
