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
        records.append({
            "key": name,
            "name": name,
            "aliases": [],
            "codes": [code],
            "is_raw": "(Ore)" in name or "(Raw)" in name,
        })
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


def load_golden() -> list[dict]:
    return yaml.safe_load(_PATH.read_text(encoding="utf-8"))


def build_golden() -> list[dict]:
    return asyncio.run(_all())
