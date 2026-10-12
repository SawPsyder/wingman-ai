"""Golden name pins. Expected values come from the frozen ranker."""

from skills.uexcorp.tests.golden_names import build_golden, load_golden


def test_golden_names_match_the_pin():
    assert build_golden() == load_golden()
