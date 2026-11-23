"""A host as the system of record for its people: the signed update and erasure, both halves."""
from __future__ import annotations

import json
import time

import jwt
import pytest

from canopy_sdk import contacts, site
from canopy_sdk.host import HostConfig
from canopy_sdk.host.contacts import erase_contacts, update_contacts
from canopy_sdk.keys import generate_private_key, private_pem

CANOPY = "https://canopy.example.org"
AUD = contacts.update_url(CANOPY)
ERASE_AUD = contacts.erase_url(CANOPY)


@pytest.fixture()
def key():
    return generate_private_key("EdDSA")


def _token(key, items, **kw):
    return contacts.update_token("connect-messaging", key, AUD, items, **kw)


def test_an_update_round_trips(key):
    item = {"sub": "personalid:ana", "display_name": "Ana", "record_uri": "labs-worker://ana",
            "profile": {"stage": "deliver"}, "version": 3}
    spent = []
    claims = contacts.verify_update(_token(key, [item], workspace="connect"), [key.public_key()],
                                    audiences=[AUD], spend_jti=spent.append)
    assert claims["contacts"] == [item] and claims["iss"] == "connect-messaging"
    assert claims["canopy_workspace"] == "connect" and spent == [claims]
    assert jwt.get_unverified_header(_token(key, [item]))["typ"] == contacts.UPDATE_TYP


def test_an_erasure_round_trips_and_dedupes(key):
    token = contacts.erase_token("connect-messaging", key, ERASE_AUD, ["a", "b", "a"])
    claims = contacts.verify_erase(token, [key.public_key()], audiences=[ERASE_AUD])
    assert claims["subs"] == ["a", "b"]


def test_an_update_cannot_be_replayed_as_an_erasure_or_the_reverse(key):
    update = _token(key, [{"sub": "a", "version": 1}])
    with pytest.raises(contacts.ContactRecordError) as exc:
        contacts.verify_erase(update, [key.public_key()], audiences=[AUD])
    assert exc.value.code == "bad_typ"
    erase = contacts.erase_token("s", key, ERASE_AUD, ["a"])
    with pytest.raises(contacts.ContactRecordError):
        contacts.verify_update(erase, [key.public_key()], audiences=[ERASE_AUD])


@pytest.mark.parametrize("change,code", [
    (dict(audiences=["https://elsewhere.example.org/api/sites/contacts"]), "wrong_audience"),
    (dict(keys="other"), "bad_signature"),
])
def test_refusals(key, change, code):
    token = _token(key, [{"sub": "a", "version": 1}])
    keys = [generate_private_key("EdDSA").public_key()] if change.get("keys") else [key.public_key()]
    with pytest.raises(contacts.ContactRecordError) as exc:
        contacts.verify_update(token, keys, audiences=change.get("audiences", [AUD]))
    assert exc.value.code == code


def test_an_expired_or_long_lived_request_is_refused(key):
    old = _token(key, [{"sub": "a", "version": 1}], now=time.time() - 3600)
    with pytest.raises(contacts.ContactRecordError) as exc:
        contacts.verify_update(old, [key.public_key()], audiences=[AUD])
    assert exc.value.code == "expired"
    from canopy_sdk.jose import sign
    from canopy_sdk.keys import public_jwk

    now = int(time.time())
    long = sign({"iss": "s", "aud": AUD, "iat": now, "exp": now + 3600, "jti": "j" * 20,
                 "contacts": [{"sub": "a", "version": 1}]}, key,
                headers={"kid": public_jwk(key)["kid"], "typ": contacts.UPDATE_TYP})
    with pytest.raises(contacts.ContactRecordError):
        contacts.verify_update(long, [key.public_key()], audiences=[AUD])


def test_at_most_a_hundred_people(key):
    with pytest.raises(contacts.ContactRecordError) as exc:
        _token(key, [{"sub": f"s{i}", "version": 1} for i in range(101)])
    assert exc.value.code == "too_many"
    with pytest.raises(contacts.ContactRecordError):
        contacts.erase_token("s", key, ERASE_AUD, [f"s{i}" for i in range(101)])


@pytest.mark.parametrize("item,code", [
    ({"sub": "", "version": 1}, "bad_sub"),
    ({"sub": " a", "version": 1}, "bad_sub"),
    ({"sub": "a" * 201, "version": 1}, "bad_sub"),
    ({"sub": "a", "version": 0}, "bad_version"),
    ({"sub": "a", "version": True}, "bad_version"),
    ({"sub": "a", "version": "3"}, "bad_version"),
    ({"sub": "a"}, "bad_version"),
    ({"sub": "a", "version": 1, "notes": "x"}, "malformed"),
    ({"sub": "a", "version": 1, "attributes": {}}, "malformed"),
    ({"sub": "a", "version": 1, "profile": ["x"]}, "bad_profile"),
    ({"sub": "a", "version": 1, "profile": {f"k{i}": 1 for i in range(41)}}, "profile_too_large"),
    ({"sub": "a", "version": 1, "profile": {"history": "x" * 4100}}, "profile_too_large"),
    ({"sub": "a", "version": 1, "display_name": "a\nb"}, "bad_display_name"),
])
def test_an_item_a_host_may_not_send(item, code):
    with pytest.raises(contacts.ContactRecordError) as exc:
        contacts.check_item(item)
    assert exc.value.code == code


def test_a_profile_at_the_bounds_is_kept():
    profile = {f"k{i:02d}": "v" for i in range(40)}
    assert contacts.check_item({"sub": "a", "version": 1, "profile": profile})["profile"] == profile
    big = {"x": "y" * (contacts.MAX_PROFILE_BYTES - len('{"x":""}'))}
    assert len(contacts.profile_bytes(big)) == contacts.MAX_PROFILE_BYTES
    contacts.check_profile(big)


def test_a_record_uri_is_under_the_sites_person_scheme_with_a_url_safe_id():
    assert contacts.check_record_uri("labs-worker://ana_01-x", "labs-worker") == "labs-worker://ana_01-x"
    assert contacts.check_record_uri("LABS-WORKER://ana", "labs-worker") == "labs-worker://ana"
    for bad, code in [("labs-result://ana", "wrong_scheme"), ("labs-worker://a/b", "bad_record_uri"),
                      ("labs-worker://a?b", "bad_record_uri"), ("labs-worker://", "bad_record_uri"),
                      ("labs-worker://" + "a" * 250, "bad_record_uri"), ("https://x.org/a", "wrong_scheme")]:
        with pytest.raises(contacts.ContactRecordError) as exc:
            contacts.check_record_uri(bad, "labs-worker")
        assert exc.value.code == code, bad
    with pytest.raises(contacts.ContactRecordError) as exc:
        contacts.check_record_uri("labs-worker://ana", "")
    assert exc.value.code == "no_person_scheme"


def _config(key):
    return HostConfig(signing_key=private_pem(key), canopy_base_url=CANOPY, app_name="connect-messaging",
                      workspace="connect")


def test_update_contacts_posts_the_jws_as_the_body(key):
    sent = []

    def post(url, body, *, content_type):
        sent.append((url, body, content_type))
        return 200, json.dumps({"results": [{"sub": "a", "contact_id": 1, "status": "created"}]}).encode(), {}

    out = update_contacts(_config(key), [{"sub": "a", "version": 1}], post=post)
    assert out["results"][0]["status"] == "created"
    url, body, content_type = sent[0]
    assert url == AUD and content_type == "application/jwt"
    claims = contacts.verify_update(body.decode(), [key.public_key()], audiences=[AUD])
    assert claims["canopy_workspace"] == "connect"


def test_erase_contacts_and_canopys_refusal_code(key):
    def post(url, body, *, content_type):
        assert url == ERASE_AUD
        return 401, json.dumps({"detail": "replayed: already used"}).encode(), {}

    with pytest.raises(contacts.ContactRecordError) as exc:
        erase_contacts(_config(key), ["a"], post=post)
    assert exc.value.code == "replayed"


def test_a_host_with_no_canopy_is_not_configured(key):
    with pytest.raises(contacts.ContactRecordError) as exc:
        update_contacts(HostConfig(signing_key=private_pem(key)), [{"sub": "a", "version": 1}])
    assert exc.value.code == "not_configured"


# --- site.json: person_scheme and retention_days -------------------------------------


def test_site_json_carries_person_scheme_and_retention_days():
    doc = site.site_document("connect-messaging", site={"person_scheme": "labs-worker", "retention_days": 90})
    assert doc["site"] == {"origins": [], "jwks_url": "", "issuer": "", "mcp_resource": "",
                           "person_scheme": "labs-worker", "retention_days": 90}
    needs = site.SiteNeeds.from_claims(doc)
    assert needs.site.person_scheme == "labs-worker" and needs.site.retention_days == 90
    assert site.SITE_FACTS[-2:] == ("person_scheme", "retention_days")


def test_a_site_that_declares_neither_reads_as_before():
    needs = site.SiteNeeds.from_claims(site.site_document("s", site={"origins": ["https://a.org"]}))
    assert needs.site.person_scheme == "" and needs.site.retention_days is None


@pytest.mark.parametrize("facts", [{"person_scheme": "https"}, {"person_scheme": "has space"},
                                   {"person_scheme": 3}, {"retention_days": 0},
                                   {"retention_days": 3651}, {"retention_days": "30"},
                                   {"retention_days": True}])
def test_a_bad_fact_makes_the_document_malformed(facts):
    with pytest.raises(site.SiteDocumentError) as exc:
        site.site_document("s", site=facts)
    assert exc.value.code == "malformed"


# --- site.json: surfaces ------------------------------------------------------------


WORKERS = {"title": "Connect Worker messaging", "sensitive": False,
           "mcp_resource": "https://labs.example.org/messaging/canopy/mcp/", "person_scheme": "labs-worker"}


def test_site_json_carries_surfaces():
    doc = site.site_document("connect-labs", surfaces={"workers": WORKERS})
    assert doc["surfaces"] == {"workers": WORKERS}
    needs = site.SiteNeeds.from_claims(doc)
    workers = needs.surface("workers")
    assert workers == site.SiteSurface(name="workers", sensitive=False, title="Connect Worker messaging",
                                       mcp_resource=WORKERS["mcp_resource"], person_scheme="labs-worker")
    assert needs.surface("staff") is None


def test_a_site_with_no_surfaces_reads_as_before():
    doc = site.site_document("connect-labs")
    assert "surfaces" not in doc and site.SiteNeeds.from_claims(doc).surfaces == ()


@pytest.mark.parametrize("surfaces", [
    {"workers": {"mcp_resource": "https://a.org/mcp/"}},          # sensitive left out: never assumed
    {"workers": {"sensitive": "false"}},
    {"Workers": {"sensitive": False}},
    {"w" * 33: {"sensitive": False}},
    {"workers": {"sensitive": False, "reply_url": "https://a.org/r/"}},
    {"workers": {"sensitive": False, "mcp_resource": "ftp://a.org/"}},
    {"workers": {"sensitive": False, "person_scheme": "https"}},
    {"workers": ["sensitive"]},
    {f"s{i}": {"sensitive": True} for i in range(site.MAX_SURFACES + 1)},
])
def test_a_bad_surface_makes_the_document_malformed(surfaces):
    with pytest.raises(site.SiteDocumentError) as exc:
        site.site_document("connect-labs", surfaces=surfaces)
    assert exc.value.code == "malformed"
