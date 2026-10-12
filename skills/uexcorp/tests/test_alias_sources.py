"""Alias strings added on top of the stored name."""

from skills.uexcorp.uexcorp.naming.aliases import item_company_alias
from skills.uexcorp.uexcorp.tool.validator import _item_records


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
