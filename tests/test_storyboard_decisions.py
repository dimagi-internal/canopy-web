"""Decision rows on a storyboard: the ace-web pattern, in Postgres."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError

from apps.storyboards.models import Storyboard, StoryboardDecision
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")


@pytest.fixture()
def ws(owner):
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    return ws


@pytest.fixture()
def board(ws):
    return Storyboard.objects.create(slug="supply", title="Connect Supply", workspace=ws,
                                     capability=Storyboard.CAP_COMMENT)


def test_decision_key_is_unique_per_board(board):
    StoryboardDecision.objects.create(storyboard=board, key="banner", question="Rename?")
    with pytest.raises(IntegrityError):
        StoryboardDecision.objects.create(storyboard=board, key="banner", question="Again?")


def test_decision_defaults(board):
    d = StoryboardDecision.objects.create(storyboard=board, key="sqlns", question="Show SQ-LNS?")
    assert d.status == "ai-default"
    assert d.options == [] and d.history == [] and d.conflict_signals == []
    assert d.confirmed is False and d.override == "" and d.scope == ""
