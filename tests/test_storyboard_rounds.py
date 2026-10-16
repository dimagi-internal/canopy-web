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


# ------------------------------------------------- the agent's read, and consume


def _closed_round_with_a_note(world):
    _open(world)
    _note(world, author_name="Sophie", author_email="s@example.org")
    assert _close(world).status_code == 200
    return FeedbackRound.objects.get(number=1)


def test_agent_reads_the_round_with_emails_and_rulings(world):
    dsvc.publish(world["board"], [ROW])
    _closed_round_with_a_note(world)
    body = world["ace"].get("/api/storyboards/supply/rounds/1").json()
    assert body["state"] == "closed" and body["notes"][0]["author_email"] == "s@example.org"
    assert body["decisions"][0]["id"] == "banner"


def test_token_holder_cannot_read_round_detail(world):
    _closed_round_with_a_note(world)
    assert Client().get(f"/api/storyboards/supply/rounds/1?t={world['token']}").status_code in (401, 404)


def test_consume_refuses_unanswered_notes(world):
    _closed_round_with_a_note(world)
    resp = world["ace"].post("/api/storyboards/supply/rounds/1/consume", {"run_id": "r1"},
                             content_type="application/json")
    assert resp.status_code == 409
    assert FeedbackRound.objects.filter(state="open").count() == 0


def test_consume_opens_the_next_round_on_new_versions(world, monkeypatch):
    from apps.feedback import services as fsvc

    rnd = _closed_round_with_a_note(world)
    for fb in rnd.notes.all():
        fsvc.resolve(fb.pk, workspace_slugs=["connect"], state="declined", note="out of scope")
    monkeypatch.setattr(board_rounds, "pinned_versions", lambda b: {"supply-rutf": 5})
    resp = world["ace"].post("/api/storyboards/supply/rounds/1/consume", {"run_id": "r1"},
                             content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["number"] == 2 and resp.json()["pinned_versions"] == {"supply-rutf": 5}
    assert FeedbackRound.objects.get(number=1).state == "consumed"
    assert FeedbackRound.objects.get(number=1).consumed_by_run == "r1"


def test_note_left_while_closed_is_adopted_by_the_next_round(world):
    from apps.feedback import services as fsvc

    rnd = _closed_round_with_a_note(world)
    _note(world, author_name="Late")
    for fb in rnd.notes.all():
        fsvc.resolve(fb.pk, workspace_slugs=["connect"], state="answered", note="done")
    world["ace"].post("/api/storyboards/supply/rounds/1/consume", {"run_id": "r1"},
                      content_type="application/json")
    assert Feedback.objects.get(author_name="Late").round.number == 2


# ------------------------------------------- create conflict, and linking a project


def _board_body(slug="fresh", **extra):
    return {"slug": slug, "title": "Fresh", "acts": [], **extra}


def test_creating_an_existing_slug_is_409(world):
    first = world["ace"].post("/api/storyboards/", _board_body(), content_type="application/json")
    assert first.status_code == 200, first.content
    again = world["ace"].post("/api/storyboards/", _board_body(), content_type="application/json")
    assert again.status_code == 409


def test_patch_links_an_existing_board_to_a_project(world):
    world["board"].agent_project = None
    world["board"].save(update_fields=["agent_project"])
    resp = world["ace"].patch("/api/storyboards/supply", {"agent_project": "ace/P9"},
                              content_type="application/json")
    assert resp.status_code == 200, resp.content
    world["board"].refresh_from_db()
    assert world["board"].agent_project_id == world["project"].pk


def test_patch_unknown_project_is_422(world):
    resp = world["ace"].patch("/api/storyboards/supply", {"agent_project": "ace/P404"},
                              content_type="application/json")
    assert resp.status_code == 422


def test_patch_project_of_another_workspace_is_422(world):
    other_ws = Workspace.objects.create(slug="other", display_name="Other",
                                        created_by=world["users"]["owner"])
    other_agent = Agent.objects.create(slug="zed", name="Zed", workspace=other_ws,
                                       owner=world["users"]["owner"])
    other = AgentProject.objects.create(agent=other_agent, ext_id="P1", name="x")
    resp = world["ace"].patch("/api/storyboards/supply", {"agent_project": str(other.pk)},
                              content_type="application/json")
    assert resp.status_code == 422
    world["board"].refresh_from_db()
    assert world["board"].agent_project_id == world["project"].pk


# ------------------------------------------------- review-page suggestions join the pool


def _review(world, **extra):
    from apps.reviews.models import ReviewRequest

    return ReviewRequest.objects.create(
        run_id="supply-rutf-2026-10-14-001", narrative_slug="supply-rutf", version=3,
        workspace=world["ws"], gate="concept_change", visibility="link", request_json={}, **extra)


def _suggest(world, review, **response):
    return world["viewer"].post(f"/api/reviews/{review.pk}/suggest/", {"response_json": {
        "decisions": {}, **response}}, content_type="application/json")


def test_review_suggestions_land_in_the_open_round(world):
    _open(world)
    review = _review(world)
    resp = _suggest(world, review, overall_feedback="Lead with the forecast", edited_scenes=[
        {"id": "overview", "title": "t", "narration": "New words", "deleted": False,
         "features": [], "feedback": "shorter"}])
    assert resp.status_code == 200, resp.content
    rows = Feedback.objects.filter(round__number=1, target_ref="supply-rutf")
    assert {(r.kind, r.anchor_id) for r in rows} == {("suggestion", "overview"), ("comment", "")}
    assert rows.get(kind="suggestion").suggested_text == "New words"
    assert rows.get(kind="suggestion").target_version == 3


def test_review_suggestion_author_is_never_an_email(world):
    _open(world)
    _suggest(world, _review(world), overall_feedback="Lead with the forecast")
    fb = Feedback.objects.get(target_ref="supply-rutf")
    assert fb.author_name == "lily" and "@" not in fb.author_name


def test_review_suggestion_still_succeeds_when_pooling_fails(world, monkeypatch):
    from apps.feedback import services as fsvc

    def boom(*a, **k):
        raise RuntimeError("pool down")

    monkeypatch.setattr(fsvc, "ingest", boom)
    review = _review(world)
    assert _suggest(world, review, overall_feedback="Still counted").status_code == 200
    review.refresh_from_db()
    assert len(review.suggestions_json) == 1


# ------------------------------------------------- task 6 leftovers


def test_close_then_consume_through_the_workspace_scoped_path(world):
    from apps.feedback import services as fsvc

    rnd = _closed_round_with_a_note(world)
    for fb in rnd.notes.all():
        fsvc.resolve(fb.pk, workspace_slugs=["connect"], state="declined", note="no")
    resp = world["ace"].post("/api/w/connect/storyboards/supply/rounds/1/consume",
                             {"run_id": "r1"}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert FeedbackRound.objects.get(number=1).state == "consumed"


def test_consume_rolls_back_when_the_next_round_cannot_open(world):
    from apps.feedback import services as fsvc

    rnd = _closed_round_with_a_note(world)
    for fb in rnd.notes.all():
        fsvc.resolve(fb.pk, workspace_slugs=["connect"], state="declined", note="no")
    world["board"].agent_project = None
    world["board"].save(update_fields=["agent_project"])
    resp = world["ace"].post("/api/storyboards/supply/rounds/1/consume", {"run_id": "r1"},
                             content_type="application/json")
    assert resp.status_code == 422
    rnd.refresh_from_db()
    assert rnd.state == "closed" and rnd.consumed_by_run == ""


def test_patch_project_errors_do_not_reveal_other_tenants(world):
    other_ws = Workspace.objects.create(slug="other2", display_name="Other2",
                                        created_by=world["users"]["owner"])
    other_agent = Agent.objects.create(slug="zed2", name="Zed2", workspace=other_ws,
                                       owner=world["users"]["owner"])
    other = AgentProject.objects.create(agent=other_agent, ext_id="P1", name="x")
    foreign = world["ace"].patch("/api/storyboards/supply", {"agent_project": str(other.pk)},
                                 content_type="application/json")
    unknown = world["ace"].patch("/api/storyboards/supply", {"agent_project": "ace/P404"},
                                 content_type="application/json")
    assert foreign.status_code == unknown.status_code == 422
    assert foreign.json()["detail"].split(":")[0] == unknown.json()["detail"].split(":")[0]
    assert "workspace" not in foreign.json()["detail"]


# ---- 7b: close race, determinism, idempotent review ingest, tenancy ----------


def test_note_racing_the_close_lands_in_no_round(world, monkeypatch):
    """current() said open, then the round closed before the ingest: the note must not
    attach to the closed round."""
    _open(world)
    rnd = FeedbackRound.objects.get()
    real = board_rounds.current

    def stale_current(board):
        found = real(board)
        FeedbackRound.objects.filter(pk=rnd.pk).update(state=FeedbackRound.CLOSED)
        return found

    monkeypatch.setattr(board_rounds, "current", stale_current)
    assert _note(world, author_name="Racer").status_code == 200
    assert Feedback.objects.get(author_name="Racer").round is None


_SUGGESTION = {"overall_feedback": "Slow down", "narration_edits": {"s1": "New words"}}


def test_review_suggestion_racing_the_close_lands_in_no_round(world, monkeypatch):
    _open(world)
    rnd = FeedbackRound.objects.get()
    real = board_rounds.current

    def stale_current(board):
        found = real(board)
        FeedbackRound.objects.filter(pk=rnd.pk).update(state=FeedbackRound.CLOSED)
        return found

    monkeypatch.setattr(board_rounds, "current", stale_current)
    ids = board_rounds.ingest_review_suggestion(
        _review(world), name="Rae", email="rae@x.org", response_json=_SUGGESTION, count=1)
    assert ids and all(Feedback.objects.get(pk=i).round is None for i in ids)


def test_review_suggestion_retry_with_the_same_count_creates_rows_once(world):
    _open(world)
    review = _review(world)
    first = board_rounds.ingest_review_suggestion(
        review, name="Rae", email="", response_json=_SUGGESTION, count=3)
    again = board_rounds.ingest_review_suggestion(
        review, name="Rae", email="", response_json=_SUGGESTION, count=3)
    assert len(first) == 2 and again == []
    assert Feedback.objects.count() == 2
    assert all(f.round is not None for f in Feedback.objects.all())


def test_review_suggestion_in_one_workspace_never_reaches_anothers_round(world):
    other_ws = Workspace.objects.create(slug="other", display_name="Other",
                                        created_by=world["users"]["owner"])
    other_board = Storyboard.objects.create(slug="supply", title="Other Supply", workspace=other_ws,
                                            agent_project=world["project"])
    act = Act.objects.create(storyboard=other_board, title="RUTF", position=0)
    Entry.objects.create(act=act, narrative_slug="supply-rutf", position=0)
    FeedbackRound.objects.create(workspace=other_ws, target_kind="storyboard", target_ref="supply",
                                 number=1)
    board_rounds.ingest_review_suggestion(
        _review(world), name="Rae", email="", response_json=_SUGGESTION, count=1)
    assert Feedback.objects.filter(workspace=other_ws).count() == 0
    assert Feedback.objects.filter(workspace=world["ws"], round__isnull=True).count() == 2


# ── can_manage: who gets the Open / Close controls ──────────────────────────


def test_can_manage_is_true_only_for_editors(world):
    def read(client, token=False):
        q = f"?t={world['token']}" if token else ""
        return client.get(f"/api/storyboards/supply{q}").json()["can_manage"]

    assert read(world["owner"]) is True
    assert read(world["ace"]) is True
    assert read(world["viewer"]) is False
    assert read(Client(), token=True) is False


# ---- final review fixes -------------------------------------------------------


def _twin_board(world):
    """A second workspace with a same-slug board; ace (an editor) belongs to both."""
    ws2 = Workspace.objects.create(slug="ws2", display_name="WS2", created_by=world["users"]["owner"])
    for u, role in ((world["users"]["owner"], WorkspaceMembership.OWNER),
                    (world["users"]["ace"], WorkspaceMembership.EDITOR)):
        WorkspaceMembership.objects.create(workspace=ws2, user=u, role=role)
    agent2 = Agent.objects.create(slug="ace2", name="ACE2", workspace=ws2,
                                  owner=world["users"]["owner"])
    project2 = AgentProject.objects.create(agent=agent2, ext_id="P1", name="WS2 demos")
    board2 = Storyboard.objects.create(slug="supply", title="WS2 Supply", workspace=ws2,
                                       capability=Storyboard.CAP_SUGGEST, agent_project=project2)
    act = Act.objects.create(storyboard=board2, title="RUTF", position=0)
    Entry.objects.create(act=act, narrative_slug="supply-rutf", position=0)
    return ws2, board2


@pytest.mark.parametrize("via", ["path", "query"])
def test_round_routes_hit_the_named_workspaces_board(world, via):
    """Two workspaces each hold a "supply" board and ace edits both. The NEWER one
    (ws2) is what an unpinned slug lookup finds first, so naming the OLDER one's
    workspace must still reach it — and never touch ws2's."""
    from apps.feedback import services as fsvc

    _twin_board(world)
    ace = world["ace"]

    def url(rest):
        return (f"/api/w/connect/storyboards/supply{rest}" if via == "path"
                else f"/api/storyboards/supply{rest}?ws=connect")

    assert ace.post(url("/rounds")).status_code == 200
    assert FeedbackRound.objects.get().workspace_id == "connect"
    FeedbackRound.objects.create(workspace_id="ws2", target_kind="storyboard",
                                 target_ref="supply", number=1)  # a decoy in ws2
    resp = ace.post(url("/rounds/1/close"), {}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    rnd = FeedbackRound.objects.get(workspace_id="connect")
    assert rnd.state == "closed" and rnd.task.project == world["project"]
    assert FeedbackRound.objects.get(workspace_id="ws2").state == "open"
    detail = ace.get(url("/rounds/1"))
    assert detail.status_code == 200 and detail.json()["state"] == "closed"
    for fb in rnd.notes.all():
        fsvc.resolve(fb.pk, workspace_slugs=["connect"], state="declined", note="no")
    consumed = ace.post(url("/rounds/1/consume"), {"run_id": "r1"}, content_type="application/json")
    assert consumed.status_code == 200, consumed.content
    assert FeedbackRound.objects.get(workspace_id="connect", number=1).state == "consumed"
    assert FeedbackRound.objects.get(workspace_id="connect", number=2).state == "open"
    assert FeedbackRound.objects.get(workspace_id="ws2").state == "open"


def test_the_close_plan_names_the_boards_workspace(world):
    _open(world)
    assert _close(world).status_code == 200
    plan = FeedbackRound.objects.get().task.plan
    assert "/rounds/1?ws=connect" in plan and "/rounds/1/consume?ws=connect" in plan


def test_an_adopted_note_with_an_email_for_a_name_never_shows_it(world):
    from apps.feedback import services as fsvc

    fsvc.ingest([{"target_kind": "storyboard", "target_ref": "supply", "kind": "comment",
                  "body": "via the pool", "author_name": "x@y.org", "channel": "web"}],
                workspace=world["ws"])
    _open(world)
    body = Client().get(f"/api/storyboards/supply?t={world['token']}").json()
    assert body["round"]["notes"][0]["author_name"] == "x"
    assert "@" not in str(body["round"])
    assert body["round"]["people_count"] == 1


def test_people_count_treats_an_email_and_its_local_part_as_one_person(world):
    _open(world)
    _note(world, author_name="sophie")
    _note(world, author_name="Other")
    Feedback.objects.filter(author_name="Other").update(author_name="sophie@example.org")
    assert board_rounds.summary(FeedbackRound.objects.get())["people_count"] == 1


def test_close_skips_a_pick_whose_option_was_republished_away(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    # An unruled row's options are replaced in place by a republish.
    dsvc.publish(world["board"], [{**ROW, "options": ["Rename and re-record", "Drop it"]}])
    resp = _close(world, close_note="Done")
    assert resp.status_code == 200, resp.content
    row = world["board"].decisions.get(key="banner")
    assert row.status == "ai-default" and row.override == ""
    task = FeedbackRound.objects.get().task
    assert "0 decisions changed" in task.title
    assert "banner" in task.ask_body and "Keep as is" in task.ask_body
    assert "no longer one of its options" in task.ask_body


def test_a_stale_pick_does_not_force_a_conflict(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    _edit(world, client=world["viewer"], token=False, action="confirm")
    dsvc.publish(world["board"], [{**ROW, "options": ["Rename and re-record", "Drop it"]}])
    resp = _close(world)
    assert resp.status_code == 200, resp.content
    assert world["board"].decisions.get(key="banner").confirmed is True


def _foreign_project(world):
    other_ws = Workspace.objects.create(slug="elsewhere", display_name="Elsewhere",
                                        created_by=world["users"]["owner"])
    agent = Agent.objects.create(slug="zed", name="Zed", workspace=other_ws,
                                 owner=world["users"]["owner"])
    return AgentProject.objects.create(agent=agent, ext_id="P1", name="theirs")


def test_open_refuses_a_project_in_another_workspace(world):
    world["board"].agent_project = _foreign_project(world)
    world["board"].save()
    resp = world["owner"].post("/api/storyboards/supply/rounds")
    assert resp.status_code == 422 and "project" in resp.json()["detail"]
    assert FeedbackRound.objects.count() == 0


def test_close_refuses_a_project_in_another_workspace(world):
    _open(world)
    world["board"].agent_project = _foreign_project(world)
    world["board"].save()
    resp = _close(world)
    assert resp.status_code == 422 and "project" in resp.json()["detail"]
    assert FeedbackRound.objects.get().state == "open"
    assert AgentTask.objects.count() == 0 and Turn.objects.count() == 0


def test_a_long_board_title_still_fits_the_task_title(world):
    from apps.agents.models import AgentTask as T

    world["board"].title = "A very long storyboard title " * 10  # 290 chars
    world["board"].save()
    _open(world)
    assert _close(world).status_code == 200
    title = FeedbackRound.objects.get().task.title
    max_len = T._meta.get_field("title").max_length
    assert len(title) <= max_len and title.endswith("0 decisions changed")


def test_a_round_caps_its_staged_edits(world, monkeypatch):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    monkeypatch.setattr(board_rounds, "MAX_STAGED_EDITS", 2)
    assert _edit(world, action="confirm", author_name="A").status_code == 200
    assert _edit(world, action="confirm", author_name="B").status_code == 200
    third = _edit(world, action="confirm", author_name="C")
    assert third.status_code == 409 and "too many staged edits" in third.json()["detail"]
    assert len(FeedbackRound.objects.get().staged_edits) == 2


def test_the_board_read_honours_the_workspace_path_pin(world):
    _twin_board(world)
    assert world["ace"].get("/api/w/connect/storyboards/supply").json()["title"] == "Connect Supply"
    assert world["ace"].get("/api/w/ws2/storyboards/supply").json()["title"] == "WS2 Supply"


# ------------------------------------------------------------------ deleting a note


def _delete(world, note_id, client=None, token=False):
    c = client or world["owner"]
    q = f"?t={world['token']}" if token else ""
    return c.delete(f"/api/storyboards/supply/notes/{note_id}{q}")


def _note_id(world):
    resp = _note(world, author_name="Guest")
    assert resp.status_code in (200, 201), resp.content
    return Feedback.objects.latest("pk").pk


def test_editor_deletes_a_guest_note_in_the_open_round(world):
    _open(world)
    nid = _note_id(world)
    rnd = board_rounds.current(world["board"])
    assert board_rounds.summary(rnd)["note_count"] == 1
    resp = _delete(world, nid)
    assert resp.status_code == 204, resp.content
    assert not Feedback.objects.filter(pk=nid).exists()
    assert board_rounds.summary(board_rounds.current(world["board"]))["note_count"] == 0


def test_viewer_cannot_delete_a_note(world):
    _open(world)
    nid = _note_id(world)
    assert _delete(world, nid, client=world["viewer"]).status_code == 403
    assert Feedback.objects.filter(pk=nid).exists()


def test_token_holder_cannot_delete_a_note(world):
    _open(world)
    nid = _note_id(world)
    assert _delete(world, nid, client=Client(), token=True).status_code in (401, 403, 404)
    assert Feedback.objects.filter(pk=nid).exists()


def test_note_in_a_closed_round_cannot_be_deleted(world):
    _open(world)
    nid = _note_id(world)
    assert _close(world).status_code == 200
    resp = _delete(world, nid)
    assert resp.status_code == 409
    assert Feedback.objects.filter(pk=nid).exists()


def test_note_from_another_board_is_404(world):
    _open(world)
    other = Storyboard.objects.create(slug="other", title="Other", workspace=world["ws"],
                                      agent_project=world["project"])
    other_rnd = board_rounds.open_round(other, by_user=world["users"]["owner"])
    fb = Feedback.objects.create(workspace=world["ws"], target_kind="storyboard", target_ref="other",
                                 kind="comment", body="elsewhere", round=other_rnd)
    assert _delete(world, fb.pk).status_code == 404
    assert _delete(world, 999999).status_code == 404
    assert Feedback.objects.filter(pk=fb.pk).exists()
