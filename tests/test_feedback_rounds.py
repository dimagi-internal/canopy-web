"""The framework half of a feedback round: generic over its target, like Feedback."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from apps.feedback import rounds
from apps.feedback import services as fsvc
from apps.feedback.models import Feedback, FeedbackRound
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
T = {"target_kind": "storyboard", "target_ref": "supply"}


@pytest.fixture()
def ws():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    return Workspace.objects.create(slug="connect", display_name="Connect", created_by=u)


def _note(ws, rnd=None, **over):
    item = {"target_kind": "narrative", "target_ref": "n1", "body": "hm", "author_name": "Lily", **over}
    (pk,) = fsvc.ingest([item], workspace=ws, round=rnd)["ids"]
    return Feedback.objects.get(pk=pk)


def test_open_numbers_rounds_and_adopts_roundless_notes(ws):
    loose = _note(ws)
    r1 = rounds.open_round(workspace=ws, **T, pinned_versions={"n1": 3},
                           adopt=Feedback.objects.filter(pk=loose.pk))
    assert r1.number == 1 and r1.state == "open" and r1.pinned_versions == {"n1": 3}
    loose.refresh_from_db()
    assert loose.round == r1


def test_only_one_open_round_per_target(ws):
    rounds.open_round(workspace=ws, **T, pinned_versions={})
    with pytest.raises(rounds.RoundError) as e:
        rounds.open_round(workspace=ws, **T, pinned_versions={})
    assert e.value.status == 409


def test_db_forbids_two_open_rounds(ws):
    FeedbackRound.objects.create(workspace=ws, **T, number=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        FeedbackRound.objects.create(workspace=ws, **T, number=2)


def test_ingest_attaches_round(ws):
    r = rounds.open_round(workspace=ws, **T, pinned_versions={})
    assert _note(ws, r).round == r


def test_stage_edit_appends_with_id(ws):
    r = rounds.open_round(workspace=ws, **T, pinned_versions={})
    e = rounds.stage_edit(r, {"key": "banner", "action": "confirm", "by_name": "Lily"})
    r.refresh_from_db()
    assert r.staged_edits == [e] and e["edit_id"] and e["at"]


def test_stage_edit_on_closed_round_is_409(ws):
    r = rounds.open_round(workspace=ws, **T, pinned_versions={})
    with transaction.atomic():
        rounds.mark_closed(rounds.lock_open(r), by_user=None, note="")
    with pytest.raises(rounds.RoundError) as e:
        rounds.stage_edit(r, {"key": "banner", "action": "confirm"})
    assert e.value.status == 409


def test_consume_refuses_new_notes_then_succeeds(ws):
    r = rounds.open_round(workspace=ws, **T, pinned_versions={})
    n = _note(ws, r)
    with transaction.atomic():
        rounds.mark_closed(rounds.lock_open(r), by_user=None, note="")
    with pytest.raises(rounds.RoundError) as e:
        rounds.mark_consumed(r, run_id="supply-2026-10-20-001")
    assert e.value.status == 409 and str(n.pk) in e.value.detail
    fsvc.resolve(n.pk, workspace_slugs=[ws.pk], state="answered", note="done", resolved_in_version=4)
    r = rounds.mark_consumed(r, run_id="supply-2026-10-20-001")
    assert r.state == "consumed" and r.consumed_by_run == "supply-2026-10-20-001"


def test_consume_requires_closed(ws):
    r = rounds.open_round(workspace=ws, **T, pinned_versions={})
    with pytest.raises(rounds.RoundError) as e:
        rounds.mark_consumed(r, run_id="x")
    assert e.value.status == 409
