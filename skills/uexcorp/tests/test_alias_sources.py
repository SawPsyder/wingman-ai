"""Alias strings added on top of the stored name."""

from skills.uexcorp.uexcorp.naming.aliases import item_company_alias, vehicle_slug_remainder
from skills.uexcorp.uexcorp.tool.validator import _item_records, _vehicle_records


class _Item:
    def __init__(self, name, slug, company):
        self._name = name
        self._slug = slug
        self._company = company

    def __str__(self):
        return self._name

    def get_slug(self):
        return self._slug

    def get_company_name(self):
        return self._company


def test_item_alias_is_company_plus_name_not_the_company_alone():
    assert item_company_alias("Behring", "P4-AR") == "Behring P4-AR"
    assert item_company_alias("", "P4-AR") is None
    assert item_company_alias("Behring", "") is None
    records = _item_records([_Item("P4-AR", "p4-ar", "Behring")])
    assert records[0]["aliases"] == ["p4-ar", "Behring P4-AR"]
    assert "Behring" not in records[0]["aliases"]


class _Vehicle:
    def __init__(self, short, full, slug):
        self._short = short
        self._full = full
        self._slug = slug

    def __str__(self):
        return self._full

    def get_name(self):
        return self._short

    def get_slug(self):
        return self._slug


def test_vehicle_slug_alias_drops_a_four_letter_maker_prefix():
    assert vehicle_slug_remainder("orig-100i", "100i", "Origin 100i") is None
    assert vehicle_slug_remainder("misc-freelancer-max", "Freelancer MAX", "MISC Freelancer MAX") == "freelancer-max"
    assert vehicle_slug_remainder("n-100i", "100i", "Origin 100i") is None
    records = _vehicle_records([_Vehicle("Freelancer MAX", "MISC Freelancer MAX", "misc-freelancer-max")])
    assert "freelancer-max" in records[0]["aliases"]
    assert "Freelancer MAX" in records[0]["aliases"]
    assert records[0]["name"] == "MISC Freelancer MAX"


def test_planet_and_orbit_alias_name_origin_and_not_the_code():
    from skills.uexcorp.uexcorp.location_blocklist import _SCOPE

    assert _SCOPE["planet"][2] == ("name_origin",)
    assert _SCOPE["orbit"][2] == ("name_origin",)
    assert "code" not in _SCOPE["planet"][2]
    assert "code" not in _SCOPE["orbit"][2]


def test_moon_alias_is_name_origin_and_not_the_code():
    from skills.uexcorp.uexcorp.location_blocklist import _SCOPE

    assert _SCOPE["moon"][2] == ("name_origin",)
    assert "code" not in _SCOPE["moon"][2]
    assert _SCOPE["city"][2] == ("code",)
    assert _SCOPE["system"][2] == ("code",)
