"""Who is in a source follows the session, and one person's exclusion outlives their place in it.

Someone added to a session, or who writes in it or starts a turn in it, after its source was made is
asked before any shared use; a removal by anyone in it holds for good, whatever happens to their
membership later, and stays theirs to see and undo."""
from __future__ import annotations

import uuid

import pytest
from django.test import Client

from apps.brain import participants, sources
from apps.brain.eligibility import eligible, eligible_sources
from apps.brain.models import SourceExclusion, SourceParticipant
from apps.canopy_sessions.models import Message, SessionParticipant
from apps.contacts.models import Contact
from apps.harness.models import Turn
from tests.fixtures.brain_world import agree, brain_world

LIVE = {"kind": "live", "id": None, "window": None, "created_at": None}

pytestmark = [pytest.mark.django_db, pytest.mark.guard]


@pytest.fixture()
def w():
    out = brain_world()
    agree(out["alice"], "project")
    agree(out["bob"], "project")
    return out


def shared_ok(w):
    return bool(eligible(w["source"], w["proj"], "learn"))


def listed(w):
    return eligible_sources(w["proj"], "learn").filter(pk=w["source"].pk).exists()


def stored(w):
    return set(SourceParticipant.objects.filter(source=w["source"]).values_list("user_id", flat=True))


def line(w, author):
    return Message.objects.create(session=w["session"], turn_index=99, role="user", plaintext="hi", author=author)


def turn_by(w, **initiator):
    return Turn.objects.create(origin=Turn.ORIGIN_API, prompt="x", idempotency_key=uuid.uuid4().hex,
                               chat_session=w["session"], **initiator)


# ---- someone new in the session --------------------------------------------------------------------

def test_carol_added_later_is_asked_even_before_any_refresh(w):
    assert shared_ok(w) and listed(w)
    SessionParticipant.objects.bulk_create([SessionParticipant(session=w["session"], user=w["carol"])])  # no signal
    assert not shared_ok(w) and not listed(w)
    agree(w["carol"], "project")
    assert shared_ok(w) and listed(w)


def test_adding_and_removing_a_participant_refreshes_the_source(w, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        sp = SessionParticipant.objects.create(session=w["session"], user=w["carol"])
    assert w["carol"].pk in stored(w)
    with django_capture_on_commit_callbacks(execute=True):
        sp.delete()
    assert w["carol"].pk not in stored(w)                   # she never wrote: nothing of hers is in it


def test_a_line_by_someone_new_counts_and_survives_their_removal(w, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        sp = SessionParticipant.objects.create(session=w["session"], user=w["carol"])
        line(w, {"name": "Carol", "user_id": w["carol"].pk})
    with django_capture_on_commit_callbacks(execute=True):
        sp.delete()
    assert w["carol"].pk in stored(w)                       # her words are still in it
    assert not shared_ok(w) and not listed(w)


def test_people_of_counts_authors_and_turn_starters_including_contacts(w):
    c = Contact.objects.create(workspace=w["ws"], email="x@example.org")
    line(w, {"name": "Carol", "user_id": w["carol"].pk})
    turn_by(w, initiator_contact=c)
    who = {(u, cid) for _h, u, cid in participants.people_of(w["session"])}
    assert (w["carol"].pk, None) in who and (None, c.pk) in who


def test_a_turn_started_by_someone_new_counts_before_and_after_refresh(w, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        turn_by(w, initiator_user=w["carol"])
    assert w["carol"].pk in stored(w)
    assert not shared_ok(w) and not listed(w)
    agree(w["carol"], "project")
    assert shared_ok(w) and listed(w)


def test_a_turn_by_someone_new_is_seen_live_without_a_refresh(w):
    Turn.objects.bulk_create([Turn(origin=Turn.ORIGIN_API, prompt="x", idempotency_key=uuid.uuid4().hex,
                                   chat_session=w["session"], initiator_user=w["carol"])])
    assert not listed(w) and not shared_ok(w)


def test_a_contact_starting_a_turn_shuts_every_shared_use(w):
    c = Contact.objects.create(workspace=w["ws"], email="y@example.org")
    Turn.objects.bulk_create([Turn(origin=Turn.ORIGIN_API, prompt="x", idempotency_key=uuid.uuid4().hex,
                                   chat_session=w["session"], initiator_contact=c)])
    assert not listed(w) and eligible(w["source"], w["proj"], "learn").reason == "contact_in_source"


# ---- an exclusion is for good ----------------------------------------------------------------------

def test_an_exclusion_outlives_the_excluder_s_removal(w):
    SourceExclusion.objects.create(user=w["bob"], source=w["source"])
    SessionParticipant.objects.filter(session=w["session"], user=w["bob"]).delete()
    sources.refresh(w["source"])
    assert w["bob"].pk not in stored(w)
    assert not shared_ok(w) and not listed(w)


def client(u):
    c = Client()
    c.force_login(u)
    return c


def test_the_excluder_still_sees_and_undoes_it_after_removal(w):
    url = f"/api/me/learning/sessions/{w['session'].pk}/"
    bob = client(w["bob"])
    assert bob.put(url).json()["excluded"] is True
    SessionParticipant.objects.filter(session=w["session"], user=w["bob"]).delete()
    sources.refresh(w["source"])
    r = bob.get(url)
    assert r.status_code == 200 and r.json() == {"session_id": str(w["session"].pk), "excluded": True, "projects": [],
                                          "source": LIVE}
    assert [x["session_id"] for x in bob.get("/api/me/learning/removed/").json()] == [str(w["session"].pk)]
    assert bob.delete(url).json()["excluded"] is False
    assert bob.get(url).status_code == 404                  # no longer in it, nothing of his in it, nothing excluded


def test_someone_taken_out_whose_words_remain_can_still_take_it_out(w):
    line(w, {"name": "Bob", "user_id": w["bob"].pk})
    SessionParticipant.objects.filter(session=w["session"], user=w["bob"]).delete()
    r = client(w["bob"]).put(f"/api/me/learning/sessions/{w['session'].pk}/")
    assert r.status_code == 200 and r.json()["excluded"] is True
    assert client(w["bob"]).put(
        f"/api/me/learning/sessions/{w['session'].pk}/projects/nope/", data='{"tier": "core"}',
        content_type="application/json").status_code == 404     # declaring needs reading it now
