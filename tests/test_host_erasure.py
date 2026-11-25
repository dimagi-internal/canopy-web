"""A host site, the system of record for its people, makes canopy forget one
(`POST /api/sites/contacts/erase`, `contacts.services.erase`), and holds its contacts'
conversations to its own retention (`site.retention_days`).

Every erasure test also proves what was NOT deleted: another contact's conversations, the
same person's record in another workspace, and the person when someone else holds them."""
from __future__ import annotations

import datetime as dt
import time
import uuid
from datetime import timedelta

import pytest
from canopy_sdk import contacts as wire
from canopy_sdk import site as site_wire
from canopy_sdk.keys import generate_private_key, public_pem
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import DeletedTranscript, Message, Session
from apps.common.encryption import encrypt_secret
from apps.contacts.models import Contact, ContactErasure, Person, PersonFact
from apps.contacts.services import erase, person_for
from apps.harness.models import Turn, TurnEvent
from apps.host_grants import contact_records, site_needs
from apps.retention import services as retention
from apps.tokens.models import AppCredential, ContactToken, HostGrant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _site(ws, owner, name="connect-messaging", key=None):
    key = key or generate_private_key("EdDSA")
    app = AppCredential.create_credential(name=name, created_by=owner, workspace=ws)
    app.public_keys = [public_pem(key)]
    app.save(update_fields=["public_keys"])
    return app, key


def _contact(w, sub, *, app=None, ws=None):
    app = app or w["app"]
    return Contact.objects.create(workspace=ws or w["ws"], app=app, external_id=sub, display_name=sub,
                                  source=Contact.SOURCE_EMBED, person=person_for(app=app, external_id=sub))


def _conversation(w, contact, words="my phone number is 555"):
    session = Session.objects.create(workspace=contact.workspace, agent=w["agent"], contact=contact,
                                     title=words, metadata={"embed_app": contact.app.name})
    Message.objects.create(session=session, turn_index=0, role=Message.USER, plaintext=words, content={})
    turn = Turn.objects.create(chat_session=session, prompt=words, initiator_contact=contact,
                               initiator_kind="contact", idempotency_key=uuid.uuid4().hex)
    TurnEvent.objects.create(turn=turn, seq=1, kind="assistant", payload={"text": words})
    return session, turn


@pytest.fixture()
def w():
    owner = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    app, key = _site(ws, owner)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner)
    return {"owner": owner, "ws": ws, "app": app, "key": key, "agent": agent}


def _erase(w, subs, *, key=None, name="connect-messaging"):
    token = wire.erase_token(name, key or w["key"], contact_records.audiences("erase")[0], subs,
                             workspace="connect")
    return Client().post(wire.ERASE_PATH, data=token, content_type="application/jwt"), token


def test_erasure_forgets_the_person_and_only_them(w):
    ana = _contact(w, "personalid:ana")
    session, turn = _conversation(w, ana)
    ContactToken.issue(app=w["app"], contact=ana, ttl_seconds=600)
    HostGrant.objects.create(app=w["app"], subject=ana.external_id, contact=ana,
                             access_token_enc=encrypt_secret("t"), resource="https://h.test/mcp/",
                             dpop_jkt="x", expires_at=timezone.now() + timedelta(minutes=5))
    PersonFact.objects.create(person=ana.person, workspace=w["ws"], kind=PersonFact.ROLE,
                              statement="a field worker in Kano")
    # Someone else, on the same site and in the same workspace.
    bo = _contact(w, "personalid:bo")
    bo_session, bo_turn = _conversation(w, bo, "bo's words")
    PersonFact.objects.create(person=bo.person, workspace=w["ws"], kind=PersonFact.ROLE, statement="bo's fact")

    r, _ = _erase(w, ["personalid:ana"])
    assert r.status_code == 200, r.content
    assert r.json() == {"results": [{"sub": "personalid:ana", "status": "erased"}]}

    assert not Contact.objects.filter(pk=ana.pk).exists()
    assert not Session.objects.filter(pk=session.pk).exists() and not Turn.objects.filter(pk=turn.pk).exists()
    assert not Message.objects.filter(plaintext__contains="555").exists()
    assert not ContactToken.objects.filter(contact_id=ana.pk).exists()
    assert not HostGrant.objects.filter(subject="personalid:ana").exists()
    assert not PersonFact.objects.filter(statement="a field worker in Kano").exists()
    assert not Person.objects.filter(external_id="personalid:ana").exists()
    tomb = ContactErasure.objects.get()
    assert (tomb.app_id, tomb.external_id, tomb.reason) == (w["app"].pk, "personalid:ana", "host")
    # Nothing of anyone else's.
    assert Session.objects.filter(pk=bo_session.pk).exists() and Turn.objects.get(pk=bo_turn.pk).prompt
    assert Message.objects.filter(session=bo_session).exists()
    assert PersonFact.objects.filter(statement="bo's fact").exists() and Contact.objects.filter(pk=bo.pk).exists()


def test_erasure_is_idempotent(w):
    _contact(w, "a")
    assert _erase(w, ["a"])[0].json()["results"][0]["status"] == "erased"
    erased_at = ContactErasure.objects.get().erased_at
    again = _erase(w, ["a", "never-known"])[0].json()["results"]
    assert [x["status"] for x in again] == ["not_found", "not_found"]
    assert ContactErasure.objects.get().erased_at == erased_at and ContactErasure.objects.count() == 1


def test_the_same_person_elsewhere_keeps_their_record(w):
    """The site's sub in another workspace is the same Person (same signer) and another
    tenant's contact: it keeps its conversation, and the Person stays."""
    other_ws = Workspace.objects.create(slug="other", display_name="Other", created_by=w["owner"])
    other_app, _ = _site(other_ws, w["owner"], key=w["key"])     # same keys: the same system
    ana = _contact(w, "a")
    ana_there = _contact(w, "a", app=other_app, ws=other_ws)
    assert ana.person_id == ana_there.person_id
    there_session, _ = _conversation(w, ana_there, "their words")
    PersonFact.objects.create(person=ana.person, workspace=other_ws, kind=PersonFact.ROLE, statement="there")
    PersonFact.objects.create(person=ana.person, workspace=w["ws"], kind=PersonFact.ROLE, statement="here")

    assert _erase(w, ["a"])[0].json()["results"][0]["status"] == "erased"
    assert Contact.objects.filter(pk=ana_there.pk).exists() and Session.objects.filter(pk=there_session.pk).exists()
    assert Person.objects.filter(pk=ana.person_id).exists()
    assert list(PersonFact.objects.values_list("statement", flat=True)) == ["there"]


def test_a_person_with_a_canopy_login_is_kept(w):
    ana = _contact(w, "a")
    ana.person.user = User.objects.create_user("ana", "ana@x.org", "pw")
    ana.person.save()
    erase(ana)
    assert Person.objects.filter(pk=ana.person_id).exists()


def test_a_site_erases_only_its_own_people(w):
    other_app, other_key = _site(w["ws"], w["owner"], name="other-site")
    theirs = _contact(w, "a", app=other_app)
    session, _ = _conversation(w, theirs)
    r, _ = _erase(w, ["a"])
    assert r.json()["results"][0]["status"] == "not_found"
    assert Contact.objects.filter(pk=theirs.pk).exists() and Session.objects.filter(pk=session.pk).exists()
    # Nor an email contact that happens to carry the id.
    mail = Contact.objects.create(workspace=w["ws"], email="a@x.org", external_id="b", source=Contact.SOURCE_EMAIL)
    assert _erase(w, ["b"])[0].json()["results"][0]["status"] == "not_found"
    assert Contact.objects.filter(pk=mail.pk).exists()


def test_a_replayed_or_foreign_erasure_is_refused(w):
    _contact(w, "a")
    r, token = _erase(w, ["a"], key=generate_private_key("EdDSA"))
    assert r.status_code == 401 and Contact.objects.filter(external_id="a").exists()
    r, token = _erase(w, ["a"])
    assert r.status_code == 200
    again = Client().post(wire.ERASE_PATH, data=token, content_type="application/jwt")
    assert again.status_code == 401 and again.json()["detail"].startswith("replayed")
    update = wire.update_token("connect-messaging", w["key"], contact_records.audiences("erase")[0],
                               [{"sub": "a", "version": 1}], workspace="connect")
    r = Client().post(wire.ERASE_PATH, data=update, content_type="application/jwt")
    assert r.status_code == 401 and r.json()["detail"].startswith("bad_typ")


def test_the_runner_cannot_ship_the_words_back(w):
    ana = _contact(w, "a")
    session, _ = _conversation(w, ana)
    Session.objects.filter(pk=session.pk).update(cli_session_id="claude-transcript-1")
    erase(ana)
    assert DeletedTranscript.objects.filter(transcript_id="claude-transcript-1").exists()


def test_a_turn_the_contact_started_outside_a_conversation_loses_its_words(w):
    ana = _contact(w, "a")
    turn = Turn.objects.create(agent=w["agent"], prompt="email body with 555", initiator_contact=ana,
                               initiator_kind="contact")
    erase(ana)
    turn.refresh_from_db()
    assert turn.prompt == "" and turn.initiator_contact_id is None and turn.content_purged_at is not None


def test_an_update_signed_before_the_erasure_cannot_bring_the_person_back(w):
    _contact(w, "a")
    late = wire.update_token("connect-messaging", w["key"], contact_records.audiences("update")[0],
                             [{"sub": "a", "display_name": "Ana", "version": 9}], workspace="connect",
                             now=time.time() - 5)
    erase(Contact.objects.get(external_id="a"))
    r = Client().post(wire.UPDATE_PATH, data=late, content_type="application/jwt")
    assert r.json()["results"][0]["status"] == "refused"
    assert r.json()["results"][0]["reason"].startswith("erased")
    assert not Contact.objects.filter(external_id="a").exists()
    # A new arrival afterwards is a new person record; the tombstone stays.
    fresh = wire.update_token("connect-messaging", w["key"], contact_records.audiences("update")[0],
                              [{"sub": "a", "version": 1}], workspace="connect", now=time.time() + 2)
    r = Client().post(wire.UPDATE_PATH, data=fresh, content_type="application/jwt")
    assert r.json()["results"][0]["status"] == "created" and ContactErasure.objects.count() == 1


# --- the site's own retention ---------------------------------------------------------------


def _days_ago(n):
    return timezone.now() - dt.timedelta(days=n)


def _age(session, days):
    Session.objects.filter(pk=session.pk).update(created_at=_days_ago(days), updated_at=_days_ago(days))
    Message.objects.filter(session=session).update(created_at=_days_ago(days))
    Turn.objects.filter(chat_session=session).update(status=Turn.DONE, created_at=_days_ago(days),
                                                     finished_at=_days_ago(days))


@pytest.fixture()
def retention_days(monkeypatch):
    days = {"connect-messaging": 30}

    def needs(app, **kw):
        if app.name not in days:
            return site_needs.Needs("x", None, reason="none")
        return site_needs.Needs("x", site_wire.SiteNeeds.from_claims(site_wire.site_document(
            app.name, site={"retention_days": days[app.name]})))

    monkeypatch.setattr(site_needs, "needs_for", needs)
    return days


@override_settings(CANOPY_RETENTION_ENFORCE=False)
def test_a_sites_retention_holds_whatever_the_workspace_switch_says(w, retention_days):
    old, _ = _conversation(w, _contact(w, "old"))
    young, _ = _conversation(w, _contact(w, "young"))
    _age(old, 45)
    _age(young, 10)
    other_app, _ = _site(w["ws"], w["owner"], name="no-days")
    theirs, _ = _conversation(w, _contact(w, "theirs", app=other_app))
    _age(theirs, 45)
    member = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["owner"], title="mine")
    Message.objects.create(session=member, turn_index=0, role=Message.USER, plaintext="member words", content={})
    _age(member, 45)

    record = retention.maybe_sweep()
    assert record is not None and record.counts["totals"]["chat_messages"] == 1
    assert not Message.objects.filter(session=old).exists()
    assert Turn.objects.get(chat_session=old).prompt == ""
    assert Message.objects.filter(session=young).exists()
    assert Message.objects.filter(session=theirs).exists() and Message.objects.filter(session=member).exists()


def test_the_shorter_of_the_site_and_the_workspace_wins(w, retention_days):
    from apps.retention.models import RetentionRule

    retention_days["connect-messaging"] = 365
    session, _ = _conversation(w, _contact(w, "a"))
    _age(session, 45)
    retention.sweep(apply=True, sensitive_only=True)
    assert Message.objects.filter(session=session).exists()         # the site's 365 days hold it
    RetentionRule.objects.create(workspace=w["ws"], principal=RetentionRule.CONTACT, keep_days=30)
    retention.sweep(apply=True)
    assert not Message.objects.filter(session=session).exists()     # the workspace's 30 are shorter
