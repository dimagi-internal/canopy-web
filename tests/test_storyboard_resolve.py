"""Resolving a storyboard: follow the current release, never 500 on an unbuilt act."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.reviews.models import ReviewRequest
from apps.storyboards.models import Act, Entry, Storyboard
from apps.storyboards.services import resolve_board
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    return Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)


@pytest.fixture()
def board(ws):
    b = Storyboard.objects.create(
        slug="ecf-supply", title="What the money bought", lede="Three acts.", workspace=ws
    )
    act = Act.objects.create(storyboard=b, title="Act one", prose="Why this first.", position=0)
    Entry.objects.create(act=act, narrative_slug="verified-monitoring", position=0)
    return b


def _publish(ws, slug: str, version: int, title: str, story: str) -> ReviewRequest:
    """A narrative version, the way canopy-web actually stores one."""
    return ReviewRequest.objects.create(
        run_id=f"{slug}-2026-07-26-00{version}",
        request_json={
            "gate": "concept_change",
            "narrative_slug": slug,
            "narration": [{"id": "the-goal", "title": title, "text": story}],
            "narrative": story,
        },
        gate="concept_change",
        visibility="link",
        workspace_id=ws.slug,
        version=version,
    )


def test_the_arc_keeps_its_shape(board):
    out = resolve_board(board)
    assert out["title"] == "What the money bought"
    assert out["lede"] == "Three acts."
    assert [a["title"] for a in out["acts"]] == ["Act one"]
    assert out["acts"][0]["prose"] == "Why this first."


def test_an_unpublished_narrative_resolves_to_a_placeholder_not_an_error(board):
    """A storyboard is usually authored BEFORE its narratives are rendered. An
    arc that 500s because act three has not been filmed yet would be useless
    exactly when you are building it."""
    entry = resolve_board(board)["acts"][0]["entries"][0]
    assert entry["narrative_slug"] == "verified-monitoring"
    assert entry["published"] is False
    assert entry["video_url"] is None


def test_an_entry_follows_the_current_version(board, ws):
    _publish(ws, "verified-monitoring", 1, "First cut", "The old story.")
    entry = resolve_board(board)["acts"][0]["entries"][0]
    assert entry["published"] is True
    assert entry["version"] == 1

    _publish(ws, "verified-monitoring", 2, "Sophie's revision", "The new story.")
    entry = resolve_board(board)["acts"][0]["entries"][0]
    assert entry["version"] == 2, "the page must FOLLOW, not freeze"
    assert "new story" in entry["lede"]


def test_entries_resolve_in_position_order(board, ws):
    act = board.acts.first()
    Entry.objects.create(act=act, narrative_slug="microplans-study-groups", position=-1)
    slugs = [e["narrative_slug"] for e in resolve_board(board)["acts"][0]["entries"]]
    assert slugs == ["microplans-study-groups", "verified-monitoring"]


def test_another_tenants_narrative_does_not_resolve(board, owner):
    """Scoping is the board's workspace — a slug collision in another tenant
    must not leak that tenant's story onto this board."""
    other = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    _publish(other, "verified-monitoring", 9, "Someone else's", "Not yours.")
    entry = resolve_board(board)["acts"][0]["entries"][0]
    assert entry["published"] is False


def test_capability_is_carried_so_the_page_knows_what_to_offer(board):
    assert resolve_board(board)["capability"] == Storyboard.CAP_READ
    board.capability = Storyboard.CAP_SUGGEST
    board.save()
    assert resolve_board(board)["capability"] == Storyboard.CAP_SUGGEST
