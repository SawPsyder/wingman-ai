"""normalize() folds a name into the index key. Display text stays as stored."""

from skills.uexcorp.uexcorp.naming.normalize import compact, normalize


def test_html_apostrophe_and_ampersand_fold():
    assert normalize("People&apos;s Alliance") == "peoples alliance"
    assert normalize("Cordry's") == "cordrys"
    assert normalize("Cordry\u2019s") == "cordrys"
    assert normalize("Dudley & Daughters") == "dudley and daughters"
    assert normalize("Dudley  Daughters") == "dudley daughters"
    assert normalize(" Rest & Relax ") == "rest and relax"


def test_combining_marks_and_dashes_fold():
    assert normalize("Y\u0101i") == "yai"
    assert normalize("Th.us'\u016bng") == "th usung"
    assert normalize("Ku\u0101'li") == "kuali"
    assert normalize("Retaliator Torpedo Module \u2013 Bow") == "retaliator torpedo module bow"


def test_compact_drops_spaces_after_folding():
    assert compact("Grim HEX") == compact("GrimHEX") == compact("Grim Hex") == "grimhex"
    assert compact("Area 18") == "area18"


def test_parentheses_stay_out_of_the_key_but_the_words_remain():
    assert normalize("Gold (Ore)") == "gold ore"
    assert normalize("Laranite (Raw)") == "laranite raw"


def test_voice_forms_cover_numbers_roman_and_lagrange():
    from skills.uexcorp.uexcorp.naming.voice import voice_forms

    assert "Area eighteen" in voice_forms("Area 18")
    assert "100i" in voice_forms("one hundred i")
    assert "one hundred i" in voice_forms("100i")
    assert "Stanton 3" in voice_forms("Stanton III", scope="planet")
    assert "Stanton 3" not in voice_forms("Stanton III", scope="vehicle")
    assert "Mk 2" not in voice_forms("Mk II")
    assert "ARC Lagrange Point 1" in voice_forms("ARC-L1")
    assert "ARC-Lone" not in voice_forms("ARC-L1")
    assert "HUR-L5" in voice_forms("HUR Lagrange Point 5")


def test_a_spoken_form_picks_that_row_and_a_shared_form_asks():
    from skills.uexcorp.uexcorp.api.llm import _decide_exact

    places = [
        {"name": "Area 18", "key": "city:18", "scope": "city", "aliases": []},
        {"name": "ARC-L1", "key": "station:70", "scope": "station", "aliases": []},
        {"name": "Stanton III", "key": "planet:3", "scope": "planet", "aliases": []},
    ]
    spoken = _decide_exact(places, "area eighteen", None, match_code=False)
    assert spoken.key == "city:18"
    assert spoken.method == "normalized"
    lagrange = _decide_exact(places, "ARC Lagrange Point 1", None, match_code=False)
    assert lagrange.key == "station:70"
    roman = _decide_exact(places, "Stanton 3", None, match_code=False)
    assert roman.key == "planet:3"

    ships = [
        {"name": "Origin 100i", "key": "Origin 100i", "aliases": ["100i"]},
        {"name": "Mirai 100i", "key": "Mirai 100i", "aliases": ["100i"]},
    ]
    shared = _decide_exact(ships, "one hundred i", None, match_code=False)
    assert shared.key is None
    assert {item["name"] for item in shared.candidates} == {"Origin 100i", "Mirai 100i"}

    edition = [{"name": "F7C Hornet Mk II", "key": "F7C Hornet Mk II", "aliases": []}]
    assert _decide_exact(edition, "Mk 2", None, match_code=False) is None
