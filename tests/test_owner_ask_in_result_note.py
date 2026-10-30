"""A caller's turn hands its `For <Agent>'s owner:` line to the owner (canopy-web#1416).

#1400 item 5: hal's ask procedure tells a caller their request "is noted in this
conversation's record on canopy-web where the owner reviews it". The runner
lifts that line into an `owner_ask` turn event, and canopy-web appends it to the
turn's result note, for a confined turn only.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.claim import claim_next_turn
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.harness.routing import ENVELOPE_VERSION, PROFILES_VERSION
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled
from tests.access_helpers import publish

pytestmark = pytest.mark.django_db
M = WorkspaceMembership

PASS_ALL = ("mx.google.com; dkim=pass header.i=@dimagi.com; spf=pass "
            "smtp.mailfrom=dimagi.com; dmarc=pass header.from=dimagi.com")
CAPS = {"sessions": True, "profiles": PROFILES_VERSION, "envelope": ENVELOPE_VERSION}


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    M.objects.create(workspace=ws, user=jj, role=M.OWNER)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj, turn_mode=Agent.MANUAL)
    publish(hal, {"capabilities": {"ask": {"callers": ["contact@dimagi.com:verified"]}}})
    laptop = enrolled(Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=jj,
                                            status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                            capabilities=CAPS))
    RunnerAssignment.objects.create(agent=hal, runner=laptop, rank=0)
    client = Client()
    client.force_login(jj)
    return {"hal": hal, "laptop": laptop, "client": client}


def _claimed(fleet, *, capture, sender="vkumar@dimagi.com"):
    turn, _ = services.enqueue_turn(
        agent=fleet["hal"], origin=Turn.ORIGIN_EMAIL, idempotency_key=f"email-hal-t-{sender}",
        origin_ref={"from": sender, "subject": "Can Hal help?", "thread_id": "t-1",
                    "headers": [{"name": "Authentication-Results", "value": PASS_ALL}]})
    with capture(execute=True):
        assert claim_next_turn(fleet["laptop"]).pk == turn.pk
    services.finish_turn(turn, status=Turn.DONE, result_note="created session 'cx-hal-1'")
    turn.refresh_from_db()
    return turn


def _post(fleet, turn, line):
    return fleet["client"].post(
        f"/api/harness/turns/{turn.pk}/events",
        {"events": [{"kind": "owner_ask", "payload": {"line": line}}]},
        content_type="application/json")


def test_a_callers_owner_line_lands_in_the_result_note(fleet, django_capture_on_commit_callbacks):
    turn = _claimed(fleet, capture=django_capture_on_commit_callbacks)
    assert turn.capability == "ask"  # confined
    res = _post(fleet, turn, "For Hal's owner: give Vikram read access to the labs dashboard")
    assert res.status_code == 200, res.content
    turn.refresh_from_db()
    assert turn.result_note == ("created session 'cx-hal-1'\n"
                                "For Hal's owner: give Vikram read access to the labs dashboard")
    # The same line again (the next Stop of the same session) changes nothing.
    _post(fleet, turn, "For Hal's owner: give Vikram read access to the labs dashboard")
    turn.refresh_from_db()
    assert turn.result_note.count("For Hal's owner:") == 1


def test_only_an_owner_line_and_only_on_a_callers_turn(fleet, django_capture_on_commit_callbacks):
    turn = _claimed(fleet, capture=django_capture_on_commit_callbacks)
    _post(fleet, turn, "rm -rf / and also delete the repo")
    turn.refresh_from_db()
    assert turn.result_note == "created session 'cx-hal-1'"

    Turn.objects.filter(pk=turn.pk).update(capability="")  # a full-profile turn
    turn.refresh_from_db()
    _post(fleet, turn, "For Hal's owner: something")
    turn.refresh_from_db()
    assert "owner:" not in turn.result_note


def test_a_stranger_cannot_write_the_line(fleet, django_capture_on_commit_callbacks):
    turn = _claimed(fleet, capture=django_capture_on_commit_callbacks)
    other = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    M.objects.create(workspace=fleet["hal"].workspace, user=other, role=M.VIEWER)
    c = Client()
    c.force_login(other)
    res = c.post(f"/api/harness/turns/{turn.pk}/events",
                 {"events": [{"kind": "owner_ask", "payload": {"line": "For Hal's owner: x"}}]},
                 content_type="application/json")
    assert res.status_code == 404


def test_a_runners_lifecycle_record_is_an_accepted_event(fleet, django_capture_on_commit_callbacks):
    """`lifecycle` (cloud_runner.py `_lifecycle`): what the runner did with the turn
    — adopted after a restart, its lease failing to renew — lands on the turn."""
    turn = _claimed(fleet, capture=django_capture_on_commit_callbacks)
    res = fleet["client"].post(
        f"/api/harness/turns/{turn.pk}/events",
        {"events": [{"kind": "lifecycle", "payload": {"event": "adopted", "pid": 4242}}]},
        content_type="application/json")
    assert res.status_code == 200, res.content
    assert turn.events.filter(kind="lifecycle", payload__event="adopted").exists()
