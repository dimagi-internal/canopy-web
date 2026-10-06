"""Contract tests for the /api/shareouts Ninja surface.

Verifies: auth (401 anon, PAT writable), batch create (201), idempotent
replace per (project, period, source), roll-up rows (null project), and
GET date/project filters.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.shareouts.models import Shareout
from apps.shareouts.schemas import ShareoutOut

User = get_user_model()


def _make_user(username="alice", email="alice@dimagi.com"):
    return User.objects.create_user(username=username, email=email, password="pw")


def _auth_client(user=None):
    c = Client()
    c.force_login(user or _make_user())
    return c


def _post(client, data):
    return client.post(
        "/api/shareouts/", data=json.dumps(data), content_type="application/json"
    )


def _item(**overrides):
    base = {
        "project_slug": "canopy-web",
        "period_start": "2026-06-03T09:00:00Z",
        "period_end": "2026-06-03T17:30:00Z",
        "title": "Shipped the shareout feed",
        "summary": "TL;DR",
        "content": "## What\nBuilt it.\n\n## Why\nTeammates need it.",
        "links": [{"label": "PR #1", "url": "https://github.com/x/y/pull/1"}],
        "all_prs": [
            {"number": 1, "title": "Add the feed", "url": "https://github.com/x/y/pull/1", "state": "MERGED"},
            {"number": 2, "title": "Fix a bug", "url": "https://github.com/x/y/pull/2", "state": "OPEN"},
        ],
        "author": "jjackson",
        "source": "canopy:shareout@2026-06-04T00:00:00",
    }
    base.update(overrides)
    return base


# --- auth -----------------------------------------------------------------


@pytest.mark.django_db
def test_list_401_anonymous():
    resp = Client().get("/api/shareouts/")
    assert resp.status_code == 401


@pytest.mark.django_db
@override_settings(REQUIRE_AUTH=True)
def test_post_pat_writable():
    from apps.tokens.models import PersonalToken

    user = User.objects.create_user(username="bot", email="bot@dimagi-ai.com")
    raw, _ = PersonalToken.create_for_user(user=user, label="shareout-writer")
    resp = Client().post(
        "/api/shareouts/",
        data=json.dumps({"shareouts": [_item()]}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    assert resp.status_code == 201
    assert resp.json()["created"] == 1


# --- create ---------------------------------------------------------------


@pytest.mark.django_db
def test_post_creates_rows():
    c = _auth_client()
    resp = _post(c, {"shareouts": [_item()]})
    assert resp.status_code == 201
    body = resp.json()
    assert body == {"created": 1, "replaced": 0}
    assert Shareout.objects.count() == 1


@pytest.mark.django_db
def test_post_rollup_has_null_project():
    c = _auth_client()
    resp = _post(c, {"shareouts": [_item(project_slug=None, title="Roll-up")]})
    assert resp.status_code == 201
    assert resp.json()["created"] == 1
    row = Shareout.objects.get()
    assert row.project_slug is None


@pytest.mark.django_db
def test_post_any_well_formed_slug_is_stored():
    """There is no project registry to check a slug against any more: any
    well-formed slug is stored as given (it used to be skipped as unknown)."""
    c = _auth_client()
    resp = _post(c, {"shareouts": [_item(project_slug="never-registered.repo")]})
    assert resp.status_code == 201
    assert resp.json() == {"created": 1, "replaced": 0}
    assert Shareout.objects.get().project_slug == "never-registered.repo"


@pytest.mark.django_db
@pytest.mark.parametrize("bad", ["has space", "-leading", "slash/in/it", "x" * 201])
def test_post_malformed_slug_is_rejected(bad):
    c = _auth_client()
    resp = _post(c, {"shareouts": [_item(project_slug=bad)]})
    assert resp.status_code == 422
    assert Shareout.objects.count() == 0


@pytest.mark.django_db
def test_post_empty_slug_is_the_rollup():
    c = _auth_client()
    resp = _post(c, {"shareouts": [_item(project_slug="")]})
    assert resp.status_code == 201
    assert Shareout.objects.get().project_slug is None


@pytest.mark.django_db
def test_post_idempotent_replace_same_group():
    c = _auth_client()
    _post(c, {"shareouts": [_item(content="v1")]})
    resp = _post(c, {"shareouts": [_item(content="v2")]})
    assert resp.status_code == 201
    assert resp.json() == {"created": 1, "replaced": 1}
    assert Shareout.objects.count() == 1
    assert Shareout.objects.get().content == "v2"


@pytest.mark.django_db
def test_post_different_period_does_not_replace():
    c = _auth_client()
    _post(c, {"shareouts": [_item(period_start="2026-06-02T09:00:00Z", period_end="2026-06-02T17:00:00Z")]})
    _post(c, {"shareouts": [_item(period_start="2026-06-03T09:00:00Z", period_end="2026-06-03T17:00:00Z")]})
    assert Shareout.objects.count() == 2


# --- list -----------------------------------------------------------------


@pytest.mark.django_db
def test_list_returns_rows_and_validates():
    c = _auth_client()
    _post(c, {"shareouts": [_item()]})
    resp = c.get("/api/shareouts/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    item = ShareoutOut.model_validate(body["items"][0])
    assert len(item.all_prs) == 2
    assert item.all_prs[0].number == 1


@pytest.mark.django_db
def test_list_project_filter_excludes_others():
    c = _auth_client()
    _post(c, {"shareouts": [_item(project_slug="canopy-web")]})
    _post(c, {"shareouts": [_item(project_slug="ace")]})
    resp = c.get("/api/shareouts/?project=ace")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["project_slug"] == "ace"


@pytest.mark.django_db
def test_list_date_filter():
    c = _auth_client()
    _post(c, {"shareouts": [_item(period_start="2026-05-01T09:00:00Z", period_end="2026-05-01T17:00:00Z")]})
    _post(c, {"shareouts": [_item(period_start="2026-06-03T09:00:00Z", period_end="2026-06-03T17:00:00Z")]})
    resp = c.get("/api/shareouts/?date_from=2026-06-01")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["period_start"].startswith("2026-06-03")


# --- clear ----------------------------------------------------------------


@pytest.mark.django_db
def test_clear_by_source():
    c = _auth_client()
    _post(c, {"shareouts": [_item(source="run-A")]})
    _post(c, {"shareouts": [_item(period_start="2026-06-04T09:00:00Z", period_end="2026-06-04T17:00:00Z", source="run-B")]})
    resp = c.post("/api/shareouts/clear/", data=json.dumps({"source": "run-A"}), content_type="application/json")
    assert resp.status_code == 200
    assert resp.json() == {"cleared": 1}
    assert Shareout.objects.count() == 1
    assert Shareout.objects.get().source == "run-B"


@pytest.mark.django_db
def test_clear_empty_body_clears_all():
    c = _auth_client()
    _post(c, {"shareouts": [_item()]})
    resp = c.post("/api/shareouts/clear/", data=json.dumps({}), content_type="application/json")
    assert resp.status_code == 200
    assert resp.json()["cleared"] >= 1
    assert Shareout.objects.count() == 0


# --- produced_by_agent (agent-aware "on behalf of the user") ---------------


@pytest.mark.django_db
def test_produced_by_agent_round_trips():
    """An agent-produced shareout records the producer; a human run omits it
    and reads back as empty."""
    client = _auth_client()
    resp = _post(client, {"shareouts": [
        _item(produced_by_agent="eva"),
        _item(project_slug=None, title="Roll-up", produced_by_agent="eva"),
    ]})
    assert resp.status_code == 201, resp.content

    rows = client.get("/api/shareouts/").json()["items"]
    assert rows, "expected shareouts back"
    assert all(r["produced_by_agent"] == "eva" for r in rows)

    # A human run (no produced_by_agent) reads back as "".
    _post(client, {"shareouts": [_item(
        project_slug="echo",
        source="canopy:shareout@human",
    )]})
    echo_row = next(r for r in client.get("/api/shareouts/?project=echo").json()["items"])
    assert echo_row["produced_by_agent"] == ""


@pytest.mark.django_db
def test_produced_by_agent_not_in_idempotency_key():
    """produced_by_agent rides along — two posts differing ONLY in it (same
    project+period+source) still dedupe to one row, latest value winning."""
    client = _auth_client()
    _post(client, {"shareouts": [_item(produced_by_agent="")]})
    _post(client, {"shareouts": [_item(produced_by_agent="eva")]})

    rows = Shareout.objects.filter(project_slug="canopy-web")
    assert rows.count() == 1, "same period+source must replace, not append"
    assert rows.first().produced_by_agent == "eva"
