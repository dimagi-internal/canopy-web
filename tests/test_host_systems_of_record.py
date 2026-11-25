"""A host site is the system of record for the people it brings
(docs/architecture/host-systems-of-record.md): it keeps canopy's copy of them current,
signed with its own key, and the agent sees who the person is at the host.

`POST /api/sites/contacts` is driven through the real route with a real signed request
(`canopy_sdk.contacts`), as a host would send it."""
from __future__ import annotations

import time

import pytest
from canopy_sdk import contacts as wire
from canopy_sdk import site as site_wire
from canopy_sdk.keys import generate_private_key, public_pem
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from apps.contacts.models import Contact
from apps.host_grants import contact_records, site_needs
from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _site(ws, owner, name="connect-messaging"):
    key = generate_private_key("EdDSA")
    app = AppCredential.create_credential(name=name, created_by=owner, workspace=ws)
    app.public_keys = [public_pem(key)]
    app.save(update_fields=["public_keys"])
    return app, key


@pytest.fixture()
def w():
    owner = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    app, key = _site(ws, owner)
    return {"owner": owner, "ws": ws, "app": app, "key": key}


@pytest.fixture()
def scheme(monkeypatch):
    """The site's verified site.json declares `labs-worker` as its person scheme."""
    doc = site_wire.SiteNeeds.from_claims(site_wire.site_document(
        "connect-messaging", site={"person_scheme": "labs-worker"}))
    monkeypatch.setattr(site_needs, "needs_for", lambda app, **kw: site_needs.Needs("x", doc))
    return "labs-worker"


def _post(token, *, path=wire.UPDATE_PATH, content_type="application/jwt"):
    return Client().post(path, data=token, content_type=content_type)


def _update(w, items, *, key=None, name="connect-messaging", workspace="connect", **kw):
    token = wire.update_token(name, key or w["key"], contact_records.audiences("update")[0], items,
                              workspace=workspace, **kw)
    return _post(token), token


def _raw(w, items, *, typ=wire.UPDATE_TYP, kind="update"):
    """A signed request carrying items the SDK's own builder would refuse to send: canopy
    must check each item itself."""
    from canopy_sdk.jose import new_jti, sign
    from canopy_sdk.keys import public_jwk

    now = int(time.time())
    claims = {"iss": "connect-messaging", "aud": contact_records.audiences(kind)[0], "iat": now,
              "exp": now + 60, "jti": new_jti(), "canopy_workspace": "connect",
              ("contacts" if kind == "update" else "subs"): items}
    return _post(sign(claims, w["key"], headers={"kid": public_jwk(w["key"])["kid"], "typ": typ}),
                 path=wire.UPDATE_PATH if kind == "update" else wire.ERASE_PATH)


def test_a_host_creates_its_person_before_they_ever_write_in(w, scheme):
    r, _ = _update(w, [{"sub": "personalid:ana", "display_name": "Ana", "record_uri": "labs-worker://ana",
                        "profile": {"stage": "deliver", "language": "fr"}, "version": 5}])
    assert r.status_code == 200, r.content
    [result] = r.json()["results"]
    assert result["status"] == "created" and "reason" not in result
    c = Contact.objects.get(pk=result["contact_id"])
    assert (c.workspace_id, c.app_id, c.external_id, c.source) == ("connect", w["app"].pk, "personalid:ana",
                                                                   Contact.SOURCE_EMBED)
    assert c.auth_result == Contact.AUTH_APP_SIGNED and c.message_count == 0
    assert c.display_name == "Ana" and c.record_uri == "labs-worker://ana"
    assert c.host_profile == {"stage": "deliver", "language": "fr"} and c.host_profile_version == 5
    assert c.host_profile_signed_at is not None
    # Keyed exactly as a visitor of that site: one person per (signer, sub).
    assert c.person is not None and c.person.external_id == "personalid:ana"
    # Recording grants nothing.
    assert not WorkspaceMembership.objects.filter(user__email="").exists()


def test_only_a_newer_version_overwrites_and_only_the_hosts_fields(w, scheme):
    _update(w, [{"sub": "a", "display_name": "Ana", "profile": {"stage": "learn"}, "version": 5}])
    c = Contact.objects.get(external_id="a")
    c.notes, c.attributes = "the workspace's own words", {"org": "LLO Foo"}
    c.save()
    r, _ = _update(w, [{"sub": "a", "display_name": "Ana B", "profile": {"stage": "deliver"}, "version": 6}])
    assert r.json()["results"][0]["status"] == "updated"
    c.refresh_from_db()
    assert (c.display_name, c.host_profile, c.host_profile_version) == ("Ana B", {"stage": "deliver"}, 6)
    assert c.notes == "the workspace's own words" and c.attributes == {"org": "LLO Foo"}
    for stale in (6, 2):
        r, _ = _update(w, [{"sub": "a", "display_name": "Old", "profile": {"stage": "x"}, "version": stale}])
        assert r.json()["results"][0]["status"] == "stale"
    c.refresh_from_db()
    assert c.display_name == "Ana B" and c.host_profile_version == 6
    # A field left out is left as it is; `record_uri: ""` and `profile: {}` clear.
    _update(w, [{"sub": "a", "record_uri": "labs-worker://a", "version": 7}])
    c.refresh_from_db()
    assert c.display_name == "Ana B" and c.record_uri == "labs-worker://a"
    _update(w, [{"sub": "a", "record_uri": "", "profile": {}, "version": 8}])
    c.refresh_from_db()
    assert c.record_uri == "" and c.host_profile == {}


def test_the_host_overwrites_the_name_a_visitor_assertion_only_fills(w, scheme):
    from apps.contacts.services import record_embed_visitor

    c = record_embed_visitor(workspace=w["ws"], app=w["app"], external_id="a", display_name="first")
    _update(w, [{"sub": "a", "display_name": "the host's name", "version": 1}])
    c.refresh_from_db()
    assert c.display_name == "the host's name"


def test_each_item_is_answered_on_its_own(w, scheme):
    r = _raw(w, [{"sub": "ok", "version": 1},
                 {"sub": "bad", "record_uri": "labs-result://x", "version": 1},
                 {"sub": "big", "profile": {"h": "x" * 5000}, "version": 1},
                 {"sub": "keys", "profile": {f"k{i}": 1 for i in range(41)}, "version": 1},
                 {"sub": "notes", "notes": "a host never writes the workspace's notes", "version": 1},
                 {"sub": "nover"}, "not a mapping"])
    results = r.json()["results"]
    assert [x["status"] for x in results] == ["created"] + ["refused"] * 6
    assert results[1]["reason"].startswith("wrong_scheme") and results[1]["contact_id"] is None
    assert results[2]["reason"].startswith("profile_too_large")
    assert results[3]["reason"].startswith("profile_too_large")
    assert results[4]["reason"].startswith("malformed") and results[5]["reason"].startswith("bad_version")
    assert list(Contact.objects.values_list("external_id", flat=True)) == ["ok"]


def test_a_record_uri_needs_the_site_to_declare_its_person_scheme(w, monkeypatch):
    monkeypatch.setattr(site_needs, "needs_for", lambda app, **kw: site_needs.Needs("x", None, reason="down"))
    r, _ = _update(w, [{"sub": "a", "record_uri": "labs-worker://a", "version": 1},
                       {"sub": "b", "display_name": "B", "version": 1}])
    assert [x["status"] for x in r.json()["results"]] == ["refused", "created"]
    assert r.json()["results"][0]["reason"].startswith("no_person_scheme")


def test_a_site_writes_only_its_own_people_never_another_sites(w, scheme):
    other, other_key = _site(w["ws"], w["owner"], name="other-site")
    theirs = Contact.objects.create(workspace=w["ws"], app=other, external_id="a", display_name="Theirs",
                                    source=Contact.SOURCE_EMBED)
    r, _ = _update(w, [{"sub": "a", "display_name": "Mine", "version": 9}])
    assert r.json()["results"][0]["status"] == "created"
    theirs.refresh_from_db()
    assert theirs.display_name == "Theirs" and theirs.host_profile_version == 0
    assert Contact.objects.filter(external_id="a").count() == 2


# --- refusals: nothing is written --------------------------------------------------------


def test_a_replayed_request_is_refused(w, scheme):
    r, token = _update(w, [{"sub": "a", "version": 1}])
    assert r.status_code == 200
    again = _post(token)
    assert again.status_code == 401 and again.json()["detail"].startswith("replayed")


def test_another_key_or_an_unknown_site_is_refused(w, scheme):
    r, _ = _update(w, [{"sub": "a", "version": 1}], key=generate_private_key("EdDSA"))
    assert r.status_code == 401 and r.json()["detail"].startswith("bad_signature")
    r, _ = _update(w, [{"sub": "a", "version": 1}], name="no-such-site")
    assert r.status_code == 401 and r.json()["detail"].startswith("unknown_issuer")
    assert not Contact.objects.exists()


def test_a_revoked_site_writes_nothing(w, scheme):
    from django.utils import timezone

    AppCredential.objects.filter(pk=w["app"].pk).update(revoked_at=timezone.now())
    r, _ = _update(w, [{"sub": "a", "version": 1}])
    assert r.status_code == 401 and not Contact.objects.exists()


def test_the_wrong_audience_type_or_lifetime_is_refused(w, scheme):
    token = wire.update_token("connect-messaging", w["key"], "https://elsewhere.example.org/api/sites/contacts",
                              [{"sub": "a", "version": 1}], workspace="connect")
    assert _post(token).json()["detail"].startswith("wrong_audience")
    erase = wire.erase_token("connect-messaging", w["key"], contact_records.audiences("update")[0], ["a"],
                             workspace="connect")
    assert _post(erase).json()["detail"].startswith("bad_typ")
    old, _ = _update(w, [{"sub": "a", "version": 1}], now=time.time() - 600)
    assert old.status_code == 401 and old.json()["detail"].startswith("expired")
    assert not Contact.objects.exists()


def test_the_body_must_be_the_jws(w, scheme):
    _, token = _update(w, [{"sub": "a", "version": 1}])
    assert _post(token, content_type="application/json").status_code == 415
    assert _post("", content_type="application/jwt").status_code in (400, 415)
    assert _post("not a jws").status_code in (400, 401)


def test_more_than_a_hundred_people_is_refused_whole(w, scheme):
    r = _raw(w, [{"sub": f"s{i}", "version": 1} for i in range(101)])
    assert r.status_code == 400 and r.json()["detail"].startswith("too_many")
    assert not Contact.objects.exists()


def test_the_route_is_public_by_signature_only():
    from apps.common.middleware import _is_public

    assert _is_public("/api/sites/contacts") and _is_public("/api/sites/contacts/erase")


# --- the agent sees who this is at the host ------------------------------------------------


def test_the_envelope_names_the_person_at_the_host(w, scheme):
    from apps.agents.models import Agent
    from apps.canopy_sessions.models import Session
    from apps.harness import caller_context
    from apps.harness.models import Turn

    r, _ = _update(w, [{"sub": "personalid:ana", "record_uri": "labs-worker://ana",
                        "profile": {"stage": "deliver"}, "version": 1}])
    contact = Contact.objects.get(pk=r.json()["results"][0]["contact_id"])
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=w["ws"], owner=w["owner"])
    session = Session.objects.create(workspace=w["ws"], agent=agent, contact=contact,
                                     metadata={"embed_app": "connect-messaging"})
    turn = Turn.objects.create(chat_session=session, prompt="hi", initiator_contact=contact,
                               initiator_kind="contact")
    block = caller_context._contact(Turn.objects.get(pk=turn.pk).initiator_contact)
    assert block["external_id"] == "personalid:ana" and block["site"] == "connect-messaging"
    assert block["record_uri"] == "labs-worker://ana" and block["host_profile"] == {"stage": "deliver"}
    assert block["person_id"] == contact.person_id is not None


def test_an_email_contact_names_no_host(w):
    from apps.harness import caller_context

    c = Contact.objects.create(workspace=w["ws"], email="x@partner.org", source=Contact.SOURCE_EMAIL)
    block = caller_context._contact(c)
    assert block["external_id"] == "" and block["site"] == "" and block["record_uri"] == ""
    assert block["host_profile"] == {}
