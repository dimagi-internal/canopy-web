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


def test_a_kept_uri_is_its_scheme_and_a_url_safe_id_and_nothing_more():
    def kept(uri, schemes=None):
        return resources.validate_link({**LINK, "uri": uri}, schemes) is not None

    assert kept("labs-result://" + "x" * 64) and not kept("labs-result://" + "x" * 65)
    for smuggled in ("labs-result://abc/rows/Amina", "labs-result://abc?who=Amina", "labs-result://a.b",
                     "labs-result://abc#Amina", "labs-result://", "labs-result:abc", "labs-result://a%20b"):
        assert not kept(smuggled), smuggled
    assert resources.MAX_URI_LENGTH == 256


def test_a_declared_id_length_is_exact():
    rid = "A" * 23 + "_"
    assert resources.validate_link({**LINK, "uri": f"labs-result://{rid}"}, {"labs-result": 24})
    for wrong in (rid[:-1], rid + "x"):
        assert resources.validate_link({**LINK, "uri": f"labs-result://{wrong}"}, {"labs-result": 24}) is None
    # A scheme declared with no length keeps the default bound.
    assert resources.validate_link({**LINK, "uri": "labs-result://abc"}, {"labs-result": None})
    assert resources.validate_link({**LINK, "uri": "labs-draft://abc"}, {"labs-result": 24}) is None
    for bad in (15, 65, "24", True, None):
        with pytest.raises(resources.ResourceError):
            resources.check_id_length(bad)


def test_a_mime_type_is_one_of_a_short_list():
    for mime in sorted(resources.MIME_TYPES):
        assert resources.validate_link({**LINK, "mimeType": mime}, None)["mimeType"] == mime
    assert resources.validate_link({**LINK, "mimeType": "Application/JSON"}, None)["mimeType"] == "application/json"
    for smuggled in ("text/amina-12-bisi-3", "application/json; rows=Amina", "x/" + "a" * 90):
        assert resources.validate_link({**LINK, "mimeType": smuggled}, None) is None, smuggled


def test_at_most_ten_links_are_kept():
    many = [{**LINK, "uri": f"labs-result://{i}"} for i in range(25)]
    assert len(resources.validate_links(many, ["labs-result"])) == resources.MAX_LINKS == 10


def test_the_builder_gives_the_model_a_description_that_canopy_does_not_keep():
    built = resources.resource_link("labs-result://abc", "r", description="2 rows")
    assert built["description"] == "2 rows"
    assert "description" not in resources.validate_link(built, ["labs-result"])
    with pytest.raises(resources.ResourceError):
        resources.resource_link("labs-result://" + "x" * 300, "r")
