"""difflib and name casefold stay inside naming/.

Permanent exceptions are concept rows 19, 21, 22, 23, 24, and 25.
Row 26 is not an exception: the star-system filter uses normalize.

The ranker still casefolds from api/llm.py, resolver.py, and
tool/validator.py. Steps 3-5 call those functions from naming/ and
left the functions where they were. That list is closed. A new
function outside it fails. model/terminal.py casefolds a terminal
type and a sort key, which are not a second name matcher.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "uexcorp"

# Rows 19 and 24: stored-entry match. Rows 21-23 and 25: prefilters and
# the legacy blacklist copy. They may keep a local comparison.
PERMANENT = frozenset({
    ("location_blocklist.py", "matching_blocks"),
    ("location_blocklist.py", "_parse_block_ref"),
    ("location_blocklist.py", "_migrate_body"),
    ("location_blocklist.py", "_copy_legacy_rows"),
    ("tool/item_information.py", "execute"),
    ("tool/location_information.py", "__get_space_stations"),
    ("handler/config_handler.py", "__sync_commodity_blacklist"),
})

# Closed set of functions that already casefold a name outside naming/.
SURVIVING = frozenset({
    ("api/llm.py", "_stoplist_blocks_pick"),
    ("api/llm.py", "_query_equals_code"),
    ("api/llm.py", "_alnum_key"),
    ("api/llm.py", "_whole_word_name"),
    ("api/llm.py", "_decide_exact"),
    ("api/llm.py", "_ambiguous_word_pick_ok"),
    ("api/llm.py", "_spellings_for"),
    ("api/llm.py", "_plain_model_pick_ok"),
    ("api/llm.py", "_strip_scope_words"),
    ("api/llm.py", "_place_score"),
    ("api/llm.py", "rank_places"),
    ("api/llm.py", "_demote_terminal_nicknames"),
    ("api/llm.py", "_fuzzy_alias_forms"),
    ("api/llm.py", "_place_contains"),
    ("api/llm.py", "shortlist_places"),
    ("api/llm.py", "reserved_station"),
    ("api/llm.py", "best_name_match"),
    ("resolver.py", "collect_locations"),
    ("resolver.py", "_parent_place"),
    ("resolver.py", "_bare_generic_place"),
    ("resolver.py", "_shop_records"),
    ("resolver.py", "_shops_of_brand"),
    ("resolver.py", "_decided_name"),
    ("resolver.py", "_shop_spoken_name"),
    ("resolver.py", "resolve_location_name"),
    ("resolver.py", "_label"),
    ("tool/validator.py", "_vehicle_records"),
    ("tool/validator.py", "_item_records"),
    ("tool/validator.py", "_commodity_code"),
    ("tool/validator.py", "_exclude_expands"),
    ("tool/validator.py", "_query_is_code"),
    ("tool/validator.py", "_code_holder_names"),
    ("tool/validator.py", "_raw_variant_names"),
    ("tool/validator.py", "_commodity_miss_note"),
    ("tool/validator.py", "_tradable_miss_note"),
    ("model/terminal.py", "commodity_terminal"),
    ("model/terminal.py", "sort_terminals"),
})


def _production_files():
    for path in sorted(ROOT.rglob("*.py")):
        if "naming" in path.parts:
            continue
        yield path


def _imports_difflib(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "difflib" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "difflib":
            return True
    return False


class _CasefoldCalls(ast.NodeVisitor):
    def __init__(self):
        self.stack: list[str] = []
        self.functions: set[str] = set()

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.stack.append("<lambda>")
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "casefold":
            named = [name for name in self.stack if name != "<lambda>"]
            self.functions.add(named[-1] if named else "<module>")
        self.generic_visit(node)


def _casefold_functions(tree: ast.AST) -> set[str]:
    finder = _CasefoldCalls()
    finder.visit(tree)
    return finder.functions


def _function_source(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(path.read_text(encoding="utf-8"), node)
            return segment or ""
    raise AssertionError(f"{path.name} has no {name}")


def test_difflib_stays_inside_naming():
    offenders = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _imports_difflib(tree):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_name_casefold_outside_naming_is_the_closed_list():
    found = set()
    defined = set()
    for path in _production_files():
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add((rel, node.name))
        for name in _casefold_functions(tree):
            found.add((rel, name))
    missing = [item for item in PERMANENT if item not in defined]
    assert missing == []
    extra = sorted(found - SURVIVING - PERMANENT)
    assert extra == []


def test_star_system_filter_uses_normalize_not_casefold():
    source = _function_source(ROOT / "location_blocklist.py", "_refresh_entry")
    assert "normalize(" in source
    assert "casefold" not in source


def test_stored_block_names_use_normalize():
    source = _function_source(ROOT / "location_blocklist.py", "matching_blocks")
    assert "normalize(" in source
