"""Feedback rounds on a storyboard (spec C)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, AgentTask, ProjectParticipant
from apps.feedback.models import Feedback, FeedbackRound
from apps.harness.models import Turn
from apps.storyboards import rounds as board_rounds
from apps.storyboards.models import Act, Entry, Storyboard
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")
    ace_user = User.objects.create_user("ace", "ace@dimagi-ai.com", "pw")
    viewer = User.objects.create_user("lily", "lily@dimagi.com", "pw", first_name="Lily")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=ace_user, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj, user=ace_user)
    project = AgentProject.objects.create(agent=agent, ext_id="P9", name="Connect Supply demos")
    board = Storyboard.objects.create(slug="supply", title="Connect Supply", workspace=ws,
                                      capability=Storyboard.CAP_SUGGEST, agent_project=project)
    act = Act.objects.create(storyboard=board, title="RUTF", position=0)
    Entry.objects.create(act=act, narrative_slug="supply-rutf", position=0)
    token = board.ensure_share_token()
    clients = {}
    for name, u in (("owner", jj), ("ace", ace_user), ("viewer", viewer)):
        c = Client()
        c.force_login(u)
        clients[name] = c
    return {"ws": ws, "board": board, "token": token, "project": project, "agent": agent,
            "users": {"owner": jj, "ace": ace_user, "viewer": viewer}, **clients}


def _open(world):
    resp = world["owner"].post("/api/storyboards/supply/rounds")
    assert resp.status_code == 200, resp.content
    return resp.json()


def _note(world, client=None, **body):
    c = client or Client()
    payload = {"narrative_slug": "supply-rutf", "anchor_id": "cut:procurements", "body": "Too fast",
               **body}
    return c.post(f"/api/storyboards/supply/feedback?t={world['token']}", payload,
                  content_type="application/json")


def test_open_requires_a_linked_project(world):
    world["board"].agent_project = None
    world["board"].save()
    resp = world["owner"].post("/api/storyboards/supply/rounds")
    assert resp.status_code == 422
    assert "project" in resp.json()["detail"]


def test_only_editors_open(world):
    assert world["viewer"].post("/api/storyboards/supply/rounds").status_code == 403


def test_open_pins_versions_and_adopts_loose_notes(world, monkeypatch):
    monkeypatch.setattr(board_rounds, "pinned_versions", lambda b: {"supply-rutf": 3})
    _note(world, author_name="Early bird")
    out = _open(world)
    assert out["number"] == 1 and out["pinned_versions"] == {"supply-rutf": 3}
    assert Feedback.objects.get(author_name="Early bird").round.number == 1


def test_guest_note_in_round_needs_a_name(world):
    _open(world)
    assert _note(world, author_name="").status_code == 422
    assert _note(world, author_name="Sophie").status_code == 200
    assert Feedback.objects.get(author_name="Sophie").round.number == 1


def test_member_note_uses_account_name_and_joins_the_project(world, monkeypatch):
    from apps.contacts import people

    sentinel = object()
    monkeypatch.setattr(people, "by_email", lambda e: sentinel if e == "lily@dimagi.com" else None)
    linked = []
    monkeypatch.setattr("apps.agents.participants.ensure",
                        lambda project, person, *, source, role="": linked.append((project, person, source)))
    _open(world)
    assert _note(world, client=world["viewer"], author_name="Not me").status_code == 200
    assert Feedback.objects.get(body="Too fast").author_name == "Lily"
    assert linked == [(world["project"], sentinel, ProjectParticipant.REVIEW)]


def test_note_anchor_must_be_known(world):
    _open(world)
    assert _note(world, author_name="S", anchor_id="decision:nope").status_code == 404
    assert _note(world, author_name="S", anchor_id="weird:thing").status_code == 422
    assert _note(world, author_name="S", anchor_id="product:procurements").status_code == 200
    assert _note(world, author_name="S", anchor_id="overview").status_code == 200  # a bare scene id


def test_note_after_close_waits_for_next_round(world):
    _open(world)
    rnd = FeedbackRound.objects.get()
    rnd.state = FeedbackRound.CLOSED
    rnd.save()
    assert _note(world, author_name="Late").status_code == 200
    assert Feedback.objects.get(author_name="Late").round is None


def test_token_holder_sees_everyones_round_notes_without_emails(world):
    _open(world)
    _note(world, author_name="Sophie", author_email="sophie@example.org")
    _note(world, client=world["viewer"], body="Name the suppliers")
    body = Client().get(f"/api/storyboards/supply?t={world['token']}").json()
    notes = body["round"]["notes"]
    assert {n["author_name"] for n in notes} == {"Sophie", "Lily"}
    assert all("author_email" not in n for n in notes)
    assert "sophie@example.org" not in str(body)


def test_outside_a_round_token_holders_see_no_notes(world):
    _note(world, author_name="Sophie")
    body = Client().get(f"/api/storyboards/supply?t={world['token']}").json()
    assert body["round"] is None and body["previous_round"] is None


def test_member_sees_mine_flag(world):
    _open(world)
    _note(world, client=world["viewer"], body="mine")
    _note(world, author_name="Sophie", body="theirs")
    notes = world["viewer"].get("/api/w/connect/storyboards/supply").json()["round"]["notes"]
    assert {n["body"]: n["mine"] for n in notes} == {"mine": True, "theirs": False}


def test_previous_round_carries_dispositions(world):
    from apps.feedback import services as fsvc

    _open(world)
    _note(world, author_name="Sophie")
    rnd = FeedbackRound.objects.get()
    fb = rnd.notes.get()
    fsvc.resolve(fb.pk, workspace_slugs=["connect"], state="answered", note="Slowed cut 1",
                 resolved_in_version=4)
    rnd.state = FeedbackRound.CONSUMED
    rnd.save()
    prev = Client().get(f"/api/storyboards/supply?t={world['token']}").json()["previous_round"]
    (n,) = prev["notes"]
    assert n["state"] == "answered" and n["disposition_note"] == "Slowed cut 1"
    assert n["resolved_in_version"] == 4


def test_people_count_ignores_blank_author_names(world):
    _open(world)
    _note(world, author_name="Sophie")
    Feedback.objects.filter(author_name="Sophie").update(author_name="")
    assert board_rounds.summary(FeedbackRound.objects.get())["people_count"] == 0


def test_round_payload_never_exposes_a_members_email(world):
    pat = User.objects.create_user("pat@dimagi.com", "pat@dimagi.com", "pw")
    WorkspaceMembership.objects.create(workspace=world["ws"], user=pat,
                                       role=WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(pat)
    assert c.post("/api/storyboards/supply/rounds").status_code == 200
    _note(world, client=c, body="from a nameless member")
    rnd = Client().get(f"/api/storyboards/supply?t={world['token']}").json()["round"]
    assert rnd["notes"] and "@" not in str(rnd)
    assert rnd["notes"][0]["author_name"] == "pat" and rnd["opened_by"] == "pat"


# --- Task 4: decision edits inside a round are staged ---

from apps.storyboards import decisions as dsvc  # noqa: E402

ROW = {"id": "banner", "question": "Rename?", "options": ["Rename and re-record", "Keep as is"],
       "recommended": "Rename and re-record", "review_ask": "required-before",
       "needed_by": "external-share"}


def _edit(world, client=None, token=True, **body):
    c = client or Client()
    q = f"?t={world['token']}" if token else ""
    return c.post(f"/api/storyboards/supply/decisions/banner/edit{q}", body,
                  content_type="application/json")


def test_in_round_a_guest_edit_is_staged_not_applied(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    resp = _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["value"] == "Rename and re-record"
    assert body["pending"][0]["by_name"] == "Sophie"
    assert world["board"].decisions.get(key="banner").status == "ai-default"


def test_in_round_a_member_edit_is_staged_too(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    _edit(world, client=world["viewer"], token=False, action="confirm")
    assert world["board"].decisions.get(key="banner").confirmed is False
    assert FeedbackRound.objects.get().staged_edits[0]["by_name"] == "Lily"


def test_guest_needs_suggest_grant_and_a_name(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    assert _edit(world, action="confirm").status_code == 422
    world["board"].capability = Storyboard.CAP_COMMENT
    world["board"].save()
    assert _edit(world, action="confirm", author_name="S").status_code == 403


def test_staged_edit_is_validated_now(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    assert _edit(world, action="pick", value="Not an option", author_name="S").status_code == 422
    assert _edit(world, action="undo", author_name="S").status_code == 409


def test_outside_a_round_a_guest_edit_is_409(world):
    dsvc.publish(world["board"], [ROW])
    resp = _edit(world, action="confirm", author_name="S")
    assert resp.status_code == 409 and "round" in resp.json()["detail"]


def test_pending_shows_on_the_board_read(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    _edit(world, action="confirm", author_name="Sophie")
    body = Client().get(f"/api/storyboards/supply?t={world['token']}").json()
    assert body["decisions"][0]["pending"][0]["by_name"] == "Sophie"


def test_nameless_member_decision_never_leaks_an_email(world):
    nameless = User.objects.create_user("nina@dimagi.com", "nina@dimagi.com", "pw")
    WorkspaceMembership.objects.create(workspace=world["ws"], user=nameless,
                                       role=WorkspaceMembership.EDITOR)
    dsvc.publish(world["board"], [ROW])
    c = Client()
    c.force_login(nameless)
    assert _edit(world, client=c, token=False, action="confirm").status_code == 200
    resp = Client().get(f"/api/storyboards/supply?t={world['token']}")
    assert resp.json()["decisions"][0]["decided_by"] == "nina"
    assert "@" not in resp.content.decode()


def test_staged_value_and_reasoning_are_capped_and_confirm_stores_no_value(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    assert _edit(world, action="write_in", value="x" * 201, author_name="S").status_code == 422
    assert _edit(world, action="pick", value="Keep as is", reasoning="r" * 2001,
                 author_name="S").status_code == 422
    assert _edit(world, action="confirm", value="ignored", author_name="S").status_code == 200
    (staged,) = FeedbackRound.objects.get().staged_edits
    assert staged["action"] == "confirm" and staged["value"] == ""


# ---------------------------------------------------------------- closing a round


def _close(world, client=None, **body):
    c = client or world["owner"]
    return c.post("/api/storyboards/supply/rounds/1/close", body, content_type="application/json")


def test_close_applies_edits_and_starts_exactly_one_turn(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    for i in range(12):
        _note(world, author_name=f"Reviewer {i}", body=f"note {i}")
    _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    resp = _close(world, close_note="Lily's team is done")
    assert resp.status_code == 200, resp.content
    rnd = FeedbackRound.objects.get()
    assert rnd.state == "closed" and rnd.task is not None
    row = world["board"].decisions.get(key="banner")
    assert row.override == "Keep as is" and row.decided_by_name == "Sophie"
    assert row.decided_by_verified is False
    task = rnd.task
    assert task.project == world["project"] and task.agent == world["agent"]
    assert "12 notes" in task.title and "1 decision changed" in task.title
    assert Turn.objects.filter(raised_from_task=task).count() == 1


def test_double_close_creates_one_task_and_one_turn(world):
    _open(world)
    _note(world, author_name="S")
    assert _close(world).status_code == 200
    second = _close(world)
    assert second.status_code == 409 and "closed" in second.json()["detail"]
    assert AgentTask.objects.count() == 1
    assert Turn.objects.count() == 1


def test_close_with_unpicked_conflict_is_409_and_changes_nothing(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    _edit(world, client=world["viewer"], token=False, action="confirm")
    resp = _close(world)
    assert resp.status_code == 409 and "banner" in resp.json()["detail"]
    assert FeedbackRound.objects.get().state == "open"
    assert world["board"].decisions.get(key="banner").status == "ai-default"
    assert AgentTask.objects.count() == 0


def test_close_with_a_pick_applies_the_chosen_edit(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    _edit(world, client=world["viewer"], token=False, action="confirm")
    lily = next(e for e in FeedbackRound.objects.get().staged_edits if e["by_name"] == "Lily")
    assert _close(world, picks={"banner": lily["edit_id"]}).status_code == 200
    row = world["board"].decisions.get(key="banner")
    assert row.confirmed is True and row.decided_by == world["users"]["viewer"]


def test_same_intent_from_several_people_is_not_a_conflict(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    _edit(world, action="confirm", author_name="Sophie")
    # Picking the recommended option IS confirming it.
    _edit(world, action="pick", value="Rename and re-record", author_name="Ama")
    resp = _close(world)
    assert resp.status_code == 200, resp.content
    assert world["board"].decisions.get(key="banner").confirmed is True


def test_only_editors_close(world):
    _open(world)
    assert _close(world, client=world["viewer"]).status_code == 403
    assert FeedbackRound.objects.get().state == "open"


def test_close_without_a_project_is_422_not_500(world):
    _open(world)
    world["board"].agent_project = None
    world["board"].save()
    resp = _close(world)
    assert resp.status_code == 422 and "project" in resp.json()["detail"]
    assert FeedbackRound.objects.get().state == "open"


def test_a_failure_after_the_turn_rolls_the_whole_close_back(world, monkeypatch):
    """`act` runs for real (task approved, turn enqueued), then blows up: the
    round must still be open, with no task and no turn left behind."""
    from apps.agents import services as agent_services

    dsvc.publish(world["board"], [ROW])
    _open(world)
    _note(world, author_name="S")
    _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    real_act = agent_services.act

    def act_then_fail(*a, **kw):
        real_act(*a, **kw)
        assert Turn.objects.count() == 1  # the turn really was enqueued
        raise RuntimeError("boom after the turn")

    monkeypatch.setattr(agent_services, "act", act_then_fail)
    board = world["board"]
    with pytest.raises(RuntimeError):
        board_rounds.close_round(board, board_rounds.current(board),
                                 by_user=world["users"]["owner"], note="")
    rnd = FeedbackRound.objects.get()
    assert rnd.state == "open" and rnd.task_id is None and rnd.closed_at is None
    assert AgentTask.objects.count() == 0 and Turn.objects.count() == 0
    assert world["board"].decisions.get(key="banner").status == "ai-default"
    monkeypatch.setattr(agent_services, "act", real_act)
    assert _close(world).status_code == 200  # and the close can simply be retried
    assert AgentTask.objects.count() == 1 and Turn.objects.count() == 1


def test_close_skips_and_reports_an_edit_on_a_row_superseded_mid_round(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    # The agent republishes mid-round and replaces the row the edit was staged on.
    dsvc.publish(world["board"], [ROW, {**ROW, "id": "banner-v2", "supersedes": "banner",
                                       "recommended": "Keep as is"}])
    resp = _close(world, close_note="Done for now")
    assert resp.status_code == 200, resp.content
    old = world["board"].decisions.get(key="banner")
    assert old.superseded_by == "banner-v2" and old.status == "ai-default"
    task = FeedbackRound.objects.get().task
    assert "0 decisions changed" in task.title
    assert task.ask_body.startswith("Done for now")
    assert "banner" in task.ask_body and "banner-v2" in task.ask_body
    assert "Sophie" in task.ask_body and "Keep as is" in task.ask_body
    assert Turn.objects.filter(raised_from_task=task).count() == 1


def test_close_with_a_pick_that_is_not_one_of_the_rows_edits_is_422(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    _edit(world, client=world["viewer"], token=False, action="confirm")
    resp = _close(world, picks={"banner": "not-an-edit"})
    assert resp.status_code == 422 and "banner" in resp.json()["detail"]
    assert FeedbackRound.objects.get().state == "open" and AgentTask.objects.count() == 0


def test_close_an_unknown_round_is_404(world):
    _open(world)
    resp = world["owner"].post("/api/storyboards/supply/rounds/7/close", {},
                               content_type="application/json")
    assert resp.status_code == 404


def test_a_closed_ask_from_act_is_409_not_500(world, monkeypatch):
    """Backstop: should `act` find the task's ask already closed (an idempotency
    replay of the round's task), the close is refused cleanly and rolled back."""
    from apps.agents import services as agent_services

    _open(world)

    def closed(*a, **kw):
        raise agent_services.ClosedAskError("ace/T1 has no open ask")

    monkeypatch.setattr(agent_services, "act", closed)
    resp = _close(world)
    assert resp.status_code == 409 and "closed" in resp.json()["detail"]
    assert FeedbackRound.objects.get().state == "open" and AgentTask.objects.count() == 0
