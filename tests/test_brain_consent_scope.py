"""A consent's scope (its workspaces, `include_new`, its standing rules) changes only when the person sends
it; a class toggle leaves it alone; a workspace the person left drops out instead of refusing the change;
and every narrowing runs the removal cascade, not just dropping a class."""
from __future__ import annotations

import json

import pytest
from django.test import Client

from apps.brain import cascade, consent
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.brain_world import brain_world, member

pytestmark = pytest.mark.django_db

URL = "/api/me/learning/"


@pytest.fixture()
def w():
    out = brain_world()
    out["other"] = Workspace.objects.create(slug="other", display_name="Other", created_by=out["owner"])
    member(out["alice"], out["other"])
    return out


@pytest.fixture()
def swept(monkeypatch):
    calls = []
    monkeypatch.setattr(cascade, "after_commit", lambda ids=None: calls.append(ids))
    return calls


def put(u, body):
    c = Client()
    c.force_login(u)
    return c.put(URL, data=json.dumps(body), content_type="application/json")


def test_a_class_toggle_keeps_the_scope_and_what_counts_as_new(w):
    consent.set_consent(w["alice"], classes=["individual"], workspaces=["other"], include_new=True)
    third = Workspace.objects.create(slug="third", display_name="Third", created_by=w["owner"])
    member(w["alice"], third)
    r = put(w["alice"], {"classes": ["individual", "system"]})
    assert r.status_code == 200, r.content
    row = consent.live(w["alice"])
    assert row.workspaces == ["other"] and row.include_new is True
    assert sorted(row.known_workspaces) == ["acme", "other"]          # third joined later: still "new", still covered
    from apps.brain.eligibility import covers

    assert covers(row, "third") and not covers(row, "acme")


def test_a_left_workspace_drops_out_instead_of_refusing(w):
    consent.set_consent(w["alice"], classes=["individual"], workspaces=["acme", "other"])
    WorkspaceMembership.objects.filter(user=w["alice"], workspace=w["other"]).delete()
    r = put(w["alice"], {"classes": ["individual"], "workspaces": ["acme", "other"]})
    assert r.status_code == 200, r.content
    assert consent.live(w["alice"]).workspaces == ["acme"]


def test_a_class_toggle_after_leaving_a_workspace_still_works(w):
    consent.set_consent(w["alice"], classes=["individual"], workspaces=["acme", "other"])
    WorkspaceMembership.objects.filter(user=w["alice"], workspace=w["other"]).delete()
    assert put(w["alice"], {"classes": ["individual", "memory"]}).status_code == 200


def test_naming_only_workspaces_you_left_never_widens_to_all(w):
    consent.set_consent(w["alice"], classes=["individual"], workspaces=["other"])
    WorkspaceMembership.objects.filter(user=w["alice"], workspace=w["other"]).delete()
    assert put(w["alice"], {"classes": ["individual"], "workspaces": ["other"]}).status_code == 422
    assert consent.live(w["alice"]).workspaces == ["other"]


def test_a_workspace_you_were_never_in_is_still_refused(w):
    assert put(w["alice"], {"classes": ["individual"], "workspaces": ["nowhere"]}).status_code == 422


def test_narrowing_the_workspaces_runs_the_cascade(w, swept):
    consent.set_consent(w["alice"], classes=["individual"])
    swept.clear()
    consent.set_consent(w["alice"], classes=["individual"], workspaces=["other"])
    assert swept and w["source"].pk in swept[-1]


def test_turning_include_new_off_runs_the_cascade(w, swept):
    consent.set_consent(w["alice"], classes=["individual"], workspaces=["other"], include_new=True)
    swept.clear()
    consent.set_consent(w["alice"], classes=["individual"], include_new=False)
    assert swept


def test_adding_a_standing_rule_runs_the_cascade(w, swept):
    consent.set_consent(w["alice"], classes=["individual"])
    swept.clear()
    consent.set_consent(w["alice"], classes=["individual"], standing_rules=[{"capability": "coach"}])
    assert swept


def test_widening_or_a_plain_toggle_does_not(w, swept):
    consent.set_consent(w["alice"], classes=["individual"], workspaces=["other"],
                        standing_rules=[{"capability": "coach"}])
    swept.clear()
    consent.set_consent(w["alice"], classes=["individual", "memory"])
    consent.set_consent(w["alice"], classes=["individual", "memory"], workspaces=[])
    consent.set_consent(w["alice"], classes=["individual", "memory"], standing_rules=[])
    assert swept == []


def test_the_individual_copy_says_coaches_read_transcripts_and_managers_never_do(w):
    c = Client()
    c.force_login(w["alice"])
    by = {k["name"]: k for k in c.get(URL).json()["classes"]}
    assert "transcript" in by["individual"]["seen_by"] and "coach" in by["individual"]["seen_by"]
    assert "never" in by["individual"]["seen_by"]
    assert "agree" in by["project"]["summary"] or "agree" in by["project"]["seen_by"]
