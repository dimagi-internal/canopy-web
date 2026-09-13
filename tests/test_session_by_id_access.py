"""By-id session reads must agree with the session LIST about who may see what.

The two disagreed. `_session_or_404` gated only on workspace membership, so any
co-tenant holding a session UUID could read a conversation the list correctly
refused to show them. The list's own predicate was subtly wrong too: it keyed
co-tenant visibility on having a `RunnerBinding` rather than on ORIGIN, and a
web-created session acquires a binding as soon as a runner picks it up — so a
private chat became co-tenant-readable the moment it started running.

That second bug is why the embedded-widget work can't ship without this: every
widget session is web-created, and every one of them gets bound.

Runner-DISCOVERED sessions stay co-tenant-visible on purpose — they are created
with no `created_by` and no participant row (`apps/harness/services.py`), so
gating them on ownership or participation would make them unreachable by
everybody, which is the whole emdash-discovered-session flow.
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session, SessionParticipant
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ws_with_two_members():
    """One workspace, two members — `owner` creates sessions, `other` is the
    co-tenant who must not be able to read them."""
    owner = User.objects.create_user("owner", "owner@dimagi.com", "pw")
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=other, workspace=ws, role=WorkspaceMembership.EDITOR)
    return owner, other, ws


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _bind(session, ws, paired_by):
    runner = Runner.objects.create(
        name=f"runner-{session.id.hex[:6]}", workspace=ws, location=Runner.LOCAL,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), paired_by=paired_by,
    )
    RunnerBinding.objects.create(
        session=session, runner=runner, session_key="k",
        last_interacted_at=timezone.now(), live_seen_at=timezone.now(),
    )
    return runner


def test_cotenant_cannot_read_someone_elses_web_session_by_id():
    owner, other, ws = _ws_with_two_members()
    s = Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB, title="mine")
    assert _client(other).get(f"/api/canopy-sessions/{s.id}").status_code == 404


def test_a_runner_binding_does_not_grant_cotenant_access_to_a_web_session():
    """The widget case. A bound web session is still private to its creator."""
    owner, other, ws = _ws_with_two_members()
    s = Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB, title="mine")
    _bind(s, ws, owner)
    assert _client(other).get(f"/api/canopy-sessions/{s.id}").status_code == 404


def test_creator_can_read_their_own_web_session():
    owner, _other, ws = _ws_with_two_members()
    s = Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB, title="mine")
    assert _client(owner).get(f"/api/canopy-sessions/{s.id}").status_code == 200


def test_invited_participant_can_read_a_web_session():
    """Multiplayer still works — by invitation, which is what SessionParticipant is."""
    owner, other, ws = _ws_with_two_members()
    s = Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB, title="shared")
    SessionParticipant.objects.create(session=s, user=other, role=SessionParticipant.EDITOR)
    assert _client(other).get(f"/api/canopy-sessions/{s.id}").status_code == 200


def test_cotenant_can_still_read_a_runner_discovered_session():
    """Deliberately visible: created with no created_by and no participant row,
    so anything stricter makes it unreachable by everyone."""
    owner, other, ws = _ws_with_two_members()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="disc")
    _bind(s, ws, owner)
    assert _client(other).get(f"/api/canopy-sessions/{s.id}").status_code == 200


def test_non_member_still_404s():
    owner, _other, ws = _ws_with_two_members()
    outsider = User.objects.create_user("out", "out@dimagi.com", "pw")
    s = Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB, title="mine")
    assert _client(outsider).get(f"/api/canopy-sessions/{s.id}").status_code == 404


def test_list_does_not_leak_a_bound_web_session_to_a_cotenant():
    """The list half of the same bug — binding must not imply visibility."""
    owner, other, ws = _ws_with_two_members()
    s = Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB, title="mine")
    _bind(s, ws, owner)
    rows = _client(other).get("/api/canopy-sessions/").json()
    assert str(s.id) not in {r["id"] for r in rows}


def test_list_and_by_id_agree_for_every_session_shape():
    """Parity, so the two predicates cannot drift apart again — the same shape of
    guard the harness uses for claim-vs-schedule (`tests/test_claim_schedule_parity`).
    """
    owner, other, ws = _ws_with_two_members()
    shapes = {
        "own_web": Session.objects.create(workspace=ws, created_by=owner,
                                          origin=Session.ORIGIN_WEB, title="a"),
        "other_web": Session.objects.create(workspace=ws, created_by=other,
                                            origin=Session.ORIGIN_WEB, title="b"),
        "runner_disc": Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="c"),
        "participating": Session.objects.create(workspace=ws, created_by=other,
                                                origin=Session.ORIGIN_WEB, title="d"),
    }
    _bind(shapes["own_web"], ws, owner)
    _bind(shapes["runner_disc"], ws, owner)
    SessionParticipant.objects.create(session=shapes["participating"], user=owner,
                                      role=SessionParticipant.EDITOR)

    c = _client(owner)
    listed = {r["id"] for r in c.get("/api/canopy-sessions/").json()}
    for label, s in shapes.items():
        gettable = c.get(f"/api/canopy-sessions/{s.id}").status_code == 200
        assert gettable is (str(s.id) in listed), f"list/by-id disagree on {label}"
