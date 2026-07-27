"""POST /api/ddd/narratives/{slug}/move/ — re-homing a narrative.

The membership gate is the part that matters: moving a narrative is a way to
read AND relocate someone else's work, so it must require membership of both
sides, and a dry run must not leak the existence of a workspace you cannot see.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.reviews.models import ReviewRequest
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

URL = "/api/ddd/narratives/create-survey-solicitation/move/"


def _post(client, body):
    return client.post(URL, json.dumps(body), content_type="application/json")


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def both(owner):
    for slug, name in (("dimagi", "Dimagi"), ("connect", "Connect")):
        ws = Workspace.objects.create(slug=slug, display_name=name, created_by=owner)
        WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    return owner


def _review(slug: str, version: int, workspace: str):
    return ReviewRequest.objects.create(
        run_id=f"{slug}-2026-07-26-{version:03d}",
        request_json={
            "gate": "concept_change",
            "narrative_slug": slug,
            "narration": [{"id": "the-goal", "title": "T", "text": f"v{version}"}],
        },
        gate="concept_change",
        visibility="link",
        workspace_id=workspace,
        version=version,
    )


def _split():
    """The real labs shape: one lineage straddling two tenants."""
    _review("create-survey-solicitation", 7, "dimagi")
    _review("create-survey-solicitation", 11, "connect")
    _review("create-survey-solicitation", 12, "dimagi")


@pytest.fixture()
def client(both):
    c = Client()
    c.force_login(both)
    return c


def test_dry_run_is_the_default_and_moves_nothing(client):
    _split()
    r = _post(client, {"to_workspace": "connect"})
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["dry_run"] is True
    assert body["reviews_to_move"] == 2
    assert ReviewRequest.objects.filter(workspace_id="dimagi").count() == 2


def test_the_plan_names_the_split(client):
    _split()
    info = _post(client, {"to_workspace": "connect"}).json()["narratives"][
        "create-survey-solicitation"
    ]
    assert info["split"] is True
    assert info["versions_by_workspace"] == {"connect": [11], "dimagi": [7, 12]}


def test_applying_consolidates_the_lineage(client):
    _split()
    r = _post(client, {"to_workspace": "connect", "dry_run": False})
    assert r.status_code == 200, r.content
    assert r.json()["dry_run"] is False
    assert set(ReviewRequest.objects.values_list("workspace_id", flat=True)) == {"connect"}


def test_it_is_idempotent(client):
    _split()
    _post(client, {"to_workspace": "connect", "dry_run": False})
    second = _post(client, {"to_workspace": "connect", "dry_run": False}).json()
    assert second["reviews_to_move"] == 0
    assert set(ReviewRequest.objects.values_list("workspace_id", flat=True)) == {"connect"}


def test_several_narratives_move_in_one_transaction(client):
    _split()
    _review("verified-monitoring", 17, "dimagi")
    r = _post(client, {"to_workspace": "connect", "dry_run": False, "also": ["verified-monitoring"]})
    assert r.status_code == 200, r.content
    assert set(ReviewRequest.objects.values_list("workspace_id", flat=True)) == {"connect"}


def test_you_cannot_move_INTO_a_workspace_you_do_not_belong_to(client, owner):
    Workspace.objects.create(slug="someone-else", display_name="Theirs", created_by=owner)
    _split()
    r = _post(client, {"to_workspace": "someone-else"})
    assert r.status_code == 403
    assert set(ReviewRequest.objects.values_list("workspace_id", flat=True)) == {
        "dimagi", "connect",
    }


def test_you_cannot_move_OUT_of_a_workspace_you_cannot_see(both, owner):
    """The dangerous direction: without this you could relocate another
    tenant's narrative into your own just by naming its slug."""
    theirs = Workspace.objects.create(slug="theirs", display_name="Theirs", created_by=owner)
    stranger = User.objects.create_user("nope", "nope@example.org", "pw")
    WorkspaceMembership.objects.create(workspace=theirs, user=stranger, role="owner")
    _review("create-survey-solicitation", 3, "theirs")

    c = Client()
    c.force_login(both)  # member of dimagi + connect, NOT theirs
    r = _post(c, {"to_workspace": "connect", "dry_run": False})
    assert r.status_code == 403
    assert ReviewRequest.objects.get().workspace_id == "theirs"


def test_an_unknown_narrative_404s(client):
    r = client.post(
        "/api/ddd/narratives/no-such-narrative/move/",
        json.dumps({"to_workspace": "connect"}),
        content_type="application/json",
    )
    assert r.status_code == 404


def test_anonymous_cannot_move_anything(both):
    _split()
    r = _post(Client(), {"to_workspace": "connect", "dry_run": False})
    assert r.status_code in (401, 403)
    assert ReviewRequest.objects.filter(workspace_id="dimagi").count() == 2
