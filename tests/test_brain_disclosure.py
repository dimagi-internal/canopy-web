"""P5: the disclosure summary equals the raw `brain.read` events it is compacted from, and is the person's."""
from __future__ import annotations

import json

import pytest
from django.test import Client

from apps.brain import disclosure, readers
from apps.brain.models import DisclosureDay
from apps.canopy_sessions.models import Session
from apps.events.models import Event
from tests.fixtures.brain_world import brain_world, member, user

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    out = brain_world()
    out["reader"] = user("reader")
    member(out["reader"], out["ws"])
    return out


def read(w, reader, cap="project-readers", session=None):
    readers.record_read(reader=reader, capability=cap, purpose="read", session=session or w["session"])


def client(u):
    c = Client()
    c.force_login(u)
    return c


def test_summary_equals_what_the_raw_events_say(w):
    second = Session.objects.create(workspace=w["ws"], created_by=w["alice"], origin=Session.ORIGIN_WEB)
    from apps.brain import sources

    sources.for_session(second)
    read(w, w["reader"])
    read(w, w["reader"])
    read(w, w["reader"], session=second)
    read(w, w["carol"], cap="coach")
    disclosure.compact()
    events = list(Event.objects.filter(source="brain.read"))
    assert sum(e.count for e in events) == 4
    for person, expect_reads in ((w["alice"], 4), (w["bob"], 3)):    # bob was only in the first session
        rows = DisclosureDay.objects.filter(person=person)
        assert sum(r.reads for r in rows) == expect_reads, person.username
    # recomputed independently from the raw rows: the same
    raw = disclosure.raw_rows()
    stored = {(d.person_id, d.capability_slug, d.day): (d.reads, d.sources) for d in DisclosureDay.objects.all()}
    assert stored == {k: (r, len(s)) for k, (r, s) in raw.items()}


def test_a_person_is_not_told_about_their_own_reads_and_nothing_holds_words(w):
    read(w, w["alice"])
    disclosure.compact()
    assert not DisclosureDay.objects.filter(person=w["alice"]).exists()
    assert DisclosureDay.objects.filter(person=w["bob"]).count() == 1
    row = DisclosureDay.objects.get(person=w["bob"])
    assert {f.name for f in DisclosureDay._meta.fields} == {"id", "person", "capability_slug", "day", "reads", "sources"}
    assert row.reads == 1


def test_compacting_twice_changes_nothing_and_follows_the_events(w):
    read(w, w["reader"])
    disclosure.compact()
    disclosure.compact()
    assert DisclosureDay.objects.count() == 2                           # alice and bob
    Event.objects.filter(source="brain.read").delete()
    disclosure.compact()
    assert DisclosureDay.objects.count() == 0


def test_the_route_is_the_persons_own_and_counts_only(w):
    read(w, w["reader"])
    r = client(w["bob"]).get("/api/me/learning/disclosure/")
    assert r.status_code == 200
    body = r.json()
    assert body == [{"day": body[0]["day"], "learner": "project-readers", "reads": 1, "sources": 1}]
    assert "plaintext" not in json.dumps(body)
    assert client(w["reader"]).get("/api/me/learning/disclosure/").json() == []   # their own reads are not shown
    assert Client().get("/api/me/learning/disclosure/").status_code in (401, 403)
    assert client(w["bob"]).get("/api/me/learning/disclosure/", HTTP_X_CANOPY_TURN_ID="t").status_code == 403


def test_the_removed_sessions_list(w):
    from apps.brain import consent

    consent.exclude(w["bob"], w["source"])
    rows = client(w["bob"]).get("/api/me/learning/removed/").json()
    assert [r["session_id"] for r in rows] == [str(w["session"].pk)]
    assert rows[0]["workspace"] == w["session"].workspace_id                   # for a link back to its page
    assert client(w["alice"]).get("/api/me/learning/removed/").json() == []
