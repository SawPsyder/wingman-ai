"""normalize() is unit-tested here and is not called by the ranker yet."""

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
