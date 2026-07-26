"""A storyboard is ordered acts over narratives, with one capability-bearing token."""
from __future__ import annotations

import pytest

from django.contrib.auth.models import User

from apps.storyboards.models import Act, Entry, Storyboard
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    return Workspace.objects.create(
        slug="dimagi", display_name="Dimagi", created_by=owner
    )


def _board(ws, **over):
    kwargs = dict(slug="ecf-supply", title="What the money bought", workspace=ws)
    kwargs.update(over)
    return Storyboard.objects.create(**kwargs)


def test_acts_and_entries_keep_their_order(ws):
    b = _board(ws)
    a2 = Act.objects.create(storyboard=b, title="The command centre", position=2)
    a1 = Act.objects.create(storyboard=b, title="Six weeks to a supply base", position=1)
    Entry.objects.create(act=a1, narrative_slug="procurement", position=1)
    Entry.objects.create(act=a1, narrative_slug="registry", position=0)

    assert [a.title for a in b.acts.all()] == [a1.title, a2.title]
    assert [e.narrative_slug for e in a1.entries.all()] == ["registry", "procurement"]


def test_a_slug_is_unique_per_workspace_not_globally(ws, owner):
    other = Workspace.objects.create(
        slug="connect", display_name="Connect", created_by=owner
    )
    _board(ws)
    assert _board(other).pk  # same slug, different tenant — fine


def test_ensure_share_token_is_idempotent(ws):
    b = _board(ws)
    first = b.ensure_share_token()
    assert first
    assert b.ensure_share_token() == first


def test_rotate_share_token_kills_the_old_link(ws):
    b = _board(ws)
    old = b.ensure_share_token()
    new = b.rotate_share_token()
    assert new != old
    assert not b.token_matches(old)
    assert b.token_matches(new)


def test_a_blank_or_absent_token_never_matches(ws):
    b = _board(ws)
    assert not b.token_matches(None)
    assert not b.token_matches("")
    b.ensure_share_token()
    assert not b.token_matches(None)
    assert not b.token_matches("")


def test_capability_defaults_to_read_only(ws):
    assert _board(ws).capability == Storyboard.CAP_READ


def test_grants_is_a_ladder(ws):
    b = _board(ws)
    t = b.ensure_share_token()

    assert b.grants(Storyboard.CAP_READ, t)
    assert not b.grants(Storyboard.CAP_COMMENT, t)

    b.capability = Storyboard.CAP_COMMENT
    b.save()
    assert b.grants(Storyboard.CAP_READ, t)
    assert b.grants(Storyboard.CAP_COMMENT, t)
    assert not b.grants(Storyboard.CAP_SUGGEST, t)

    b.capability = Storyboard.CAP_SUGGEST
    b.save()
    assert b.grants(Storyboard.CAP_SUGGEST, t)


def test_grants_refuses_a_wrong_token_at_every_level(ws):
    b = _board(ws, capability=Storyboard.CAP_SUGGEST)
    b.ensure_share_token()
    for cap in (Storyboard.CAP_READ, Storyboard.CAP_COMMENT, Storyboard.CAP_SUGGEST):
        assert not b.grants(cap, "not-the-token")


def test_pinning_an_entry_is_possible_but_not_the_default(ws):
    b = _board(ws)
    act = Act.objects.create(storyboard=b, title="Act", position=1)
    e = Entry.objects.create(act=act, narrative_slug="verified-monitoring", position=0)
    assert e.pinned_run_id == ""


def test_deleting_a_board_takes_its_acts_and_entries(ws):
    b = _board(ws)
    act = Act.objects.create(storyboard=b, title="Act", position=1)
    Entry.objects.create(act=act, narrative_slug="verified-monitoring", position=0)
    b.delete()
    assert Act.objects.count() == 0
    assert Entry.objects.count() == 0
