"""The dialog a blocked agent is waiting on, from report to phone.

The bug this closes: on 2026-07-31 `ace`'s `spark` session blocked on an
`AskUserQuestion` for 52 minutes. The menu reached nothing — it was produced
only as a view-only live frame (never persisted, never in the connect
snapshot), and only ever by a chat send failing against it, so opening the
session showed a chat that had simply stopped. See
`canopy_transcript.questions` for why the transcript rather than the screen is
now the source.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

MENU = {
    "question": "How should the run proceed?",
    "title": "Phase 3→4",
    "body": "",
    "selected": None,
    "options": [
        {"number": 1, "label": "Proceed to Phase 4", "description": "carry on"},
        {"number": 2, "label": "Stop the run here", "description": "end here"},
    ],
    "source": "transcript",
}


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def _ws(slug, owner):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


def _runner(pairer, ws):
    return Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", paired_by=pairer, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )


def _report(client, runner_id, sessions):
    return client.post(
        f"/api/harness/runners/{runner_id}/sessions",
        {"sessions": sessions},
        content_type="application/json",
    )


def _setup():
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    client = Client()
    client.force_login(jj)
    return jj, ws, runner, client


# -- the report writes it through ------------------------------------------


def test_a_reported_question_lands_on_the_binding():
    _jj, _ws, runner, client = _setup()
    assert _report(client, runner.id, [
        {"emdash_task": "spark", "project": "ace", "question": MENU},
    ]).status_code == 200
    binding = RunnerBinding.objects.get(session_key="spark")
    assert binding.pending_question["question"] == "How should the run proceed?"
    assert [o["number"] for o in binding.pending_question["options"]] == [1, 2]


def test_answering_at_the_laptop_clears_it_on_the_next_report():
    """The report is a fresh observation every ~10s, so "no dialog" has to be
    able to retire one. Otherwise a menu answered at the keyboard keeps live
    buttons on every phone, and a tap types a number at what is now a prompt."""
    _jj, _ws, runner, client = _setup()
    _report(client, runner.id, [{"emdash_task": "spark", "project": "ace", "question": MENU}])
    _report(client, runner.id, [{"emdash_task": "spark", "project": "ace", "question": None}])
    assert RunnerBinding.objects.get(session_key="spark").pending_question is None


def test_a_runner_that_never_heard_of_this_still_reports_normally():
    """An old runner omits the field. That lands as "no dialog", which is the
    safe direction — a phone with no buttons, never buttons against a dialog
    that is not there."""
    _jj, _ws, runner, client = _setup()
    assert _report(client, runner.id, [
        {"emdash_task": "spark", "project": "ace"},
    ]).status_code == 200
    assert RunnerBinding.objects.get(session_key="spark").pending_question is None


# -- the phone can see it --------------------------------------------------


def test_the_session_list_flags_a_session_waiting_on_a_human():
    """"The session stopped" was the whole symptom. A list that cannot say
    "this one is waiting on you" leaves a blocked agent looking identical to an
    idle one."""
    _jj, _ws, runner, client = _setup()
    _report(client, runner.id, [
        {"emdash_task": "spark", "project": "ace", "question": MENU},
        {"emdash_task": "quiet", "project": "ace", "question": None},
    ])
    rows = {r["title"]: r for r in client.get("/api/canopy-sessions/").json()}
    assert rows["spark"]["waiting_on_you"] is True
    assert rows["quiet"]["waiting_on_you"] is False


def test_opening_the_session_returns_the_menu():
    """The fix for "when I click on the session I don't see the menu". The live
    `session.activity` frame is view-only and exists only for a client that was
    already connected when it fired; the menu has to survive being looked at
    later."""
    _jj, _ws, runner, client = _setup()
    _report(client, runner.id, [{"emdash_task": "spark", "project": "ace", "question": MENU}])
    session = Session.objects.get(runner_binding__session_key="spark")
    body = client.get(f"/api/canopy-sessions/{session.id}").json()
    assert body["menu"]["question"] == "How should the run proceed?"
    assert body["menu"]["options"][0]["label"] == "Proceed to Phase 4"


def test_a_session_with_no_dialog_carries_no_menu():
    _jj, _ws, runner, client = _setup()
    _report(client, runner.id, [{"emdash_task": "quiet", "project": "ace"}])
    session = Session.objects.get(runner_binding__session_key="quiet")
    assert client.get(f"/api/canopy-sessions/{session.id}").json()["menu"] is None


def test_an_unbound_session_has_no_menu_and_does_not_explode():
    """A web chat that has never been sent has no binding at all — no box, no
    screen, nothing to be blocked on."""
    jj, ws, _runner, client = _setup()
    session = Session.objects.create(workspace=ws, title="fresh", created_by=jj)
    body = client.get(f"/api/canopy-sessions/{session.id}").json()
    assert body["menu"] is None
    assert body["waiting_on_you"] is False


# -- the WS snapshot, which is what ChatPage actually reads ------------------


def test_the_connect_snapshot_carries_the_menu():
    """`ChatPage` renders `socket.state.menu`, and the reducer only ever learned
    it from a live frame. Seeding it from the snapshot is what makes a menu
    survive a reconnect — or a phone that was never connected when it fired."""
    from apps.canopy_sessions import serializers

    jj, ws, runner, client = _setup()
    _report(client, runner.id, [{"emdash_task": "spark", "project": "ace", "question": MENU}])
    session = Session.objects.select_related("runner_binding").get(
        runner_binding__session_key="spark")
    dto = serializers.session_state_dto(
        session=session, current_user_id=jj.id, participants=[], present_ids=[],
        draft=None, messages=[],
    )
    assert dto["menu"]["question"] == "How should the run proceed?"


def test_the_snapshot_shape_is_stable_when_nothing_is_pending():
    """The field is always present. A client that has to distinguish "absent"
    from "null" grows a second code path for the same fact."""
    from apps.canopy_sessions import serializers

    jj, ws, _runner, _client = _setup()
    session = Session.objects.create(workspace=ws, title="fresh", created_by=jj)
    dto = serializers.session_state_dto(
        session=session, current_user_id=jj.id, participants=[], present_ids=[],
        draft=None, messages=[],
    )
    assert "menu" in dto and dto["menu"] is None


# -- you are TOLD, rather than having to go and look ------------------------
#
# NOTE the `django_capture_on_commit_callbacks` fixture on every test here. The
# push fires from `transaction.on_commit`, which NEVER runs under the default
# non-transactional test DB — so without it these tests pass while asserting
# nothing, including the one that checks a push failure is survivable.


def _sent(monkeypatch):
    from apps.push import services as push_services

    calls = []
    monkeypatch.setattr(push_services, "send_to_user",
                        lambda user, **kw: calls.append((user, kw)) or 1)
    return calls


def test_a_new_question_pushes_to_the_runner_s_owner(
        monkeypatch, django_capture_on_commit_callbacks):
    """The half no rendering fix covers. A perfectly rendered menu still needs
    somebody to open the app; spark's 52 minutes were 52 minutes of nobody
    knowing there was anything to open.

    A runner-discovered session has NO agent, so the audience has to fall back
    to whoever paired the runner — otherwise the exact case that motivated this
    notifies nobody."""
    sent = _sent(monkeypatch)
    _jj, _ws, runner, client = _setup()
    with django_capture_on_commit_callbacks(execute=True):
        _report(client, runner.id, [
            {"emdash_task": "spark", "project": "ace", "question": MENU}])

    assert len(sent) == 1, "nobody was told the agent is waiting"
    user, kw = sent[0]
    assert user.username == "jj"                        # the runner's pairer
    assert kw["body"] == "How should the run proceed?"  # the question itself
    session = Session.objects.get(runner_binding__session_key="spark")
    # The CHAT, not /supervisor: the tap has to land on the buttons.
    assert kw["url"] == f"/w/dimagi/chat/{session.id}"


def test_the_same_question_does_not_push_again_every_ten_seconds(
        monkeypatch, django_capture_on_commit_callbacks):
    """The report repeats on a ~10s heartbeat. Re-pushing an unchanged dialog
    would turn one agent waiting into a notification every ten seconds."""
    sent = _sent(monkeypatch)
    _jj, _ws, runner, client = _setup()
    for _ in range(3):
        with django_capture_on_commit_callbacks(execute=True):
            _report(client, runner.id, [
                {"emdash_task": "spark", "project": "ace", "question": MENU}])
    assert len(sent) == 1


def test_answering_does_not_push(monkeypatch, django_capture_on_commit_callbacks):
    """A retraction is not news — and the UI it would correct has already been
    corrected by the session.menu frame."""
    sent = _sent(monkeypatch)
    _jj, _ws, runner, client = _setup()
    for question in (MENU, None):
        with django_capture_on_commit_callbacks(execute=True):
            _report(client, runner.id, [
                {"emdash_task": "spark", "project": "ace", "question": question}])
    assert len(sent) == 1


def test_a_long_question_is_truncated_for_a_lock_screen(
        monkeypatch, django_capture_on_commit_callbacks):
    sent = _sent(monkeypatch)
    _jj, _ws, runner, client = _setup()
    long_menu = {**MENU, "question": "Why " * 200}
    with django_capture_on_commit_callbacks(execute=True):
        _report(client, runner.id, [
            {"emdash_task": "spark", "project": "ace", "question": long_menu}])
    body = sent[0][1]["body"]
    assert len(body) <= 140 and body.endswith("…")


def test_a_push_failure_never_costs_the_session_report(
        monkeypatch, django_capture_on_commit_callbacks):
    """This runs inside the liveness report. A notification must never be the
    reason the fleet stops reporting which sessions are alive."""
    from apps.push import services as push_services

    def _boom(*a, **kw):
        raise RuntimeError("push service down")

    monkeypatch.setattr(push_services, "send_to_user", _boom)

    _jj, _ws, runner, client = _setup()
    with django_capture_on_commit_callbacks(execute=True):
        assert _report(client, runner.id, [
            {"emdash_task": "spark", "project": "ace", "question": MENU},
        ]).status_code == 200
    assert RunnerBinding.objects.get(session_key="spark").pending_question is not None


def test_a_waiting_session_sorts_above_everything(monkeypatch):
    """Activity ordering BURIES it: a session stops writing the instant it asks,
    so the longer somebody has been kept waiting the further down it sinks."""
    _sent(monkeypatch)
    _jj, _ws, runner, client = _setup()
    _report(client, runner.id, [
        {"emdash_task": "chatty", "project": "ace", "question": None},
        {"emdash_task": "louder", "project": "ace", "question": None},
        {"emdash_task": "spark", "project": "ace", "question": MENU},
    ])
    titles = [r["title"] for r in client.get("/api/canopy-sessions/").json()]
    assert titles[0] == "spark", f"the waiting session sank to {titles.index('spark')}"


# -- the phone can answer it -----------------------------------------------


def test_an_answer_reaches_a_paused_runner(monkeypatch):
    """Pause stops STARTING work, never finishing it — and a blocked agent is
    unfinished work already running. PAUSED is only ever served while the
    heartbeat is fresh (a parked box that dies reads STALE), so a paused runner
    is by construction still reporting — it is the runner that delivered this
    very menu, since session reports run before the pause gate — and its control
    channel is up. Refusing it turned every tap into a dead button the moment a
    box was parked: found live 2026-07-31, first real use (jj-mbp-cdp parked for
    a rate limit, ada blocked on AskUserQuestion, every answer 'unavailable')."""
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish",
                        lambda g, m: published.append((g, m)))
    jj, ws, runner, client = _setup()
    runner.paused = True
    runner.save(update_fields=["paused"])
    _report(client, runner.id, [
        {"emdash_task": "spark", "project": "ace", "question": MENU},
    ])
    session = RunnerBinding.objects.get(session_key="spark").session

    assert runner.live_status == Runner.PAUSED
    res = client.post(
        f"/api/canopy-sessions/{session.id}/answer-menu",
        {"option": 1}, content_type="application/json",
    )
    assert res.json() == {"ok": True, "reason": ""}
    frames = [m for _g, m in published if m.get("type") == "runner.menu_answer"]
    assert frames and frames[0]["option"] == 1
    assert frames[0]["session_key"] == "spark"


def test_an_answer_to_a_dead_runner_is_still_refused(monkeypatch):
    """The widening stops at liveness: a stale heartbeat means nobody is there
    to press anything, paused or not, and 'sent' would be a lie."""
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish",
                        lambda g, m: published.append((g, m)))
    jj, ws, runner, client = _setup()
    _report(client, runner.id, [
        {"emdash_task": "spark", "project": "ace", "question": MENU},
    ])
    session = RunnerBinding.objects.get(session_key="spark").session
    runner.paused = True
    runner.last_heartbeat_at = timezone.now() - timezone.timedelta(hours=1)
    runner.save(update_fields=["paused", "last_heartbeat_at"])

    assert runner.live_status == Runner.STALE
    res = client.post(
        f"/api/canopy-sessions/{session.id}/answer-menu",
        {"option": 1}, content_type="application/json",
    )
    assert res.json() == {"ok": False, "reason": "unavailable"}
    assert not [m for _g, m in published if m.get("type") == "runner.menu_answer"]
