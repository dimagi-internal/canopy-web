"""What canopy keeps of a host's `resource_link`: an address, bounded, and nothing else."""
from __future__ import annotations

import pytest

from canopy_sdk import resources

LINK = {"type": "resource_link", "uri": "labs-result://abc", "name": "Visits by worker",
        "mimeType": "application/json", "description": "1840 rows: Amina 12, Bisi 3",
        "_meta": {"rows": [["Amina", 12]]}, "annotations": {"audience": ["user"]}}


def test_a_kept_link_is_its_address_and_never_its_description_or_meta():
    assert resources.validate_link(LINK, ["labs-result"]) == {
        "type": "resource_link", "uri": "labs-result://abc", "name": "Visits by worker",
        "mimeType": "application/json"}


def test_in_a_sensitive_conversation_the_name_is_the_scheme():
    kept = resources.validate_link(LINK, ["labs-result"], keep_name=False)
    assert kept["name"] == "labs-result" and "Visits" not in str(kept)


def test_a_long_uri_is_not_kept():
    assert resources.validate_link({**LINK, "uri": "labs-result://" + "x" * 242}, None) is not None  # 256
    assert resources.validate_link({**LINK, "uri": "labs-result://" + "x" * 243}, None) is None
    assert resources.MAX_URI_LENGTH == 256


def test_at_most_ten_links_are_kept():
    many = [{**LINK, "uri": f"labs-result://{i}"} for i in range(25)]
    assert len(resources.validate_links(many, ["labs-result"])) == resources.MAX_LINKS == 10


def test_the_builder_gives_the_model_a_description_that_canopy_does_not_keep():
    built = resources.resource_link("labs-result://abc", "r", description="2 rows")
    assert built["description"] == "2 rows"
    assert "description" not in resources.validate_link(built, ["labs-result"])
    with pytest.raises(resources.ResourceError):
        resources.resource_link("labs-result://" + "x" * 300, "r")
