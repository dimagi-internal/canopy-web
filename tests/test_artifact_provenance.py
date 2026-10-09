"""Artifacts know what made them and whether anyone reacted (board task hal/T76).

Origin: an upload/create stamps the canopy session + turn it came from (from the
X-Canopy-Parent-* headers every canopy uploader already sends) and the agent
project the work served (explicit, else the DDD run doc, else the parent turn's
board task). Reaction: comment count, distinct commenters, distinct human
viewers, and when the creator's HUMAN last opened it.
"""
from __future__ import annotations

import itertools
import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, override_settings

from apps.agent_runs.models import AgentRun
from apps.agents.models import Agent, AgentProject, AgentTask
from apps.canopy_sessions.models import Session
from apps.feedback import reactions
from apps.feedback.models import ArtifactView, Feedback
from apps.harness.models import Turn
from apps.reviews.models import ReviewRequest
from apps.storyboards.models import Storyboard
from apps.walkthroughs import storage
from apps.walkthroughs.models import Walkthrough
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.fake_drive import FakeDriveClient

pytestmark = pytest.mark.django_db
User = get_user_model()
_N = itertools.count(1)

DRIVE = dict(
    WALKTHROUGHS_ENABLED=True,
    CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder",
    CANOPY_DRIVE_SA_KEY_JSON='{"x":"y"}',
)


@pytest.fixture(autouse=True)
def fake_drive(monkeypatch):
    inst = FakeDriveClient()
    monkeypatch.setattr(storage, "get_drive_client", lambda: inst)
    return inst


@pytest.fixture()
def world():
    """Jonathan owns workspace `connect` and agent `hal`; hal has its own login,
    a project P5 and a task T76 in it; a chat session and a turn dispatched from
    that task are running."""
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    hal_user = User.objects.create_user("hal", "hal@dimagi-ai.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=hal_user, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj, user=hal_user)
    project = AgentProject.objects.create(agent=agent, ext_id="P5", name="Canopy usefulness")
    task = AgentTask.objects.create(
        agent=agent, ext_id="T76", title="link artifacts", origin="manual", project=project,
        idempotency_key=str(uuid.uuid4()),
    )
    session = Session.objects.create(workspace=ws, agent=agent, created_by=jj, title="turn")
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key=f"t76-{next(_N)}",
        raised_from_task=task,
    )
    hal = Client()
    hal.force_login(hal_user)
    human = Client()
    human.force_login(jj)
    parent = {"HTTP_X_CANOPY_PARENT_TURN": str(turn.pk),
              "HTTP_X_CANOPY_PARENT_SESSION": str(session.pk)}
    return dict(jj=jj, hal_user=hal_user, ws=ws, agent=agent, project=project, task=task,
                session=session, turn=turn, hal=hal, human=human, parent=parent)


def _upload(client, *, headers=None, **fields):
    data = {
        "file": SimpleUploadedFile("slideshow.html", b"<html></html>", content_type="text/html"),
        "title": "demo", "kind": "html", "visibility": "link", **fields,
    }
    with override_settings(**DRIVE):
        resp = client.post("/api/w/connect/walkthroughs/", data=data, format="multipart",
                           **(headers or {}))
    assert resp.status_code == 201, resp.content
    return resp.json()


def _list(client, path, **params):
    with override_settings(**DRIVE):
        resp = client.get(path, params)
    assert resp.status_code == 200, resp.content
    return resp.json()


# ----------------------------------------------------------------- origin


def test_upload_stamps_session_turn_and_board_project_from_the_headers(world):
    body = _upload(world["hal"], headers=world["parent"])

    assert body["turn_id"] == str(world["turn"].pk)
    assert body["session_id"] == str(world["session"].pk)
    assert body["agent_project"] == {"id": world["project"].pk, "agent": "hal",
                                     "ext_id": "P5", "name": "Canopy usefulness"}
    w = Walkthrough.objects.get(pk=body["id"])
    assert (w.source_turn_id, w.source_session_id, w.agent_project_id) == (
        world["turn"].pk, world["session"].pk, world["project"].pk)


def test_project_comes_from_the_ddd_run_doc_when_no_turn_names_one(world):
    AgentRun.objects.create(agent=world["agent"], kind="ddd", ext_id="supply-2026-10-09-001",
                            subject="supply", project=world["project"])
    body = _upload(world["hal"], run_id="supply-2026-10-09-001", role="deck")
    assert body["agent_project"]["ext_id"] == "P5"
    assert body["session_id"] is None and body["turn_id"] is None


def test_explicit_project_wins_and_a_bare_ext_id_means_the_callers_own_agent(world):
    other = AgentProject.objects.create(agent=world["agent"], ext_id="P9", name="other")
    body = _upload(world["hal"], agent_project="P9",
                   headers=world["parent"])
    assert body["agent_project"]["id"] == other.pk
    assert _upload(world["hal"], agent_project="hal/P5")["agent_project"]["ext_id"] == "P5"


def test_origin_in_a_workspace_the_caller_is_not_in_is_dropped_not_refused(world):
    stranger = User.objects.create_user("x", "x@dimagi.com", "pw")
    elsewhere = Workspace.objects.create(slug="elsewhere", display_name="E", created_by=stranger)
    agent = Agent.objects.create(slug="spy", name="Spy", workspace=elsewhere, owner=stranger)
    foreign = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="foreign")
    body = _upload(world["hal"], headers={"HTTP_X_CANOPY_PARENT_TURN": str(foreign.pk)},
                   agent_project="spy/P1")
    assert body["turn_id"] is None and body["agent_project"] is None


def test_a_bad_session_id_never_fails_the_upload(world):
    body = _upload(world["hal"], session_id="not-a-uuid")
    assert body["session_id"] is None


def test_walkthrough_list_filters_by_session_turn_and_agent_project(world):
    hit = _upload(world["hal"], headers=world["parent"])
    _upload(world["hal"])  # made outside any turn
    path = "/api/w/connect/walkthroughs/"
    by = lambda **p: [r["id"] for r in _list(world["human"], path, **p)]  # noqa: E731
    assert by(session=str(world["session"].pk)) == [hit["id"]]
    assert by(turn=str(world["turn"].pk)) == [hit["id"]]
    assert by(agent_project="hal/P5") == [hit["id"]]
    assert by(agent_project="hal:P5") == [hit["id"]]
    assert by(agent_project=str(world["project"].pk)) == [hit["id"]]
    assert by(session="garbage") == []
    assert len(by()) == 2


# ---------------------------------------------------------------- reaction


def test_reaction_counts_comments_commenters_and_the_owners_human_view(world):
    body = _upload(world["hal"])
    wid = body["id"]
    assert (body["comment_count"], body["viewer_count"], body["owner_viewed_at"]) == (0, 0, None)

    for email in ("a@x.org", "a@x.org", "b@x.org"):
        Feedback.objects.create(target_kind="walkthrough", target_ref=wid, body="nice",
                                author_email=email, workspace=world["ws"])

    # hal reading its own upload back (verify-link --as-hal) is not a reaction.
    world["hal"].get(f"/api/w/connect/walkthroughs/{wid}/")
    row = _list(world["human"], "/api/w/connect/walkthroughs/")[0]
    assert (row["comment_count"], row["commenter_count"]) == (3, 2)
    assert row["viewer_count"] == 0 and row["owner_viewed_at"] is None

    # Jonathan — the owner of the agent that uploaded it — opens it.
    with override_settings(**DRIVE):
        world["human"].get(f"/api/w/connect/walkthroughs/{wid}/")
    row = _list(world["human"], "/api/w/connect/walkthroughs/")[0]
    assert row["viewer_count"] == 1
    assert row["owner_viewed_at"] is not None


def test_a_walkthrough_counts_feedback_on_the_narrative_version_it_renders(world):
    review = ReviewRequest.objects.create(
        run_id="supply-2026-10-09-001", narrative_slug="supply", version=2, gate="concept_change",
        request_json={}, owner=world["hal_user"], workspace=world["ws"])
    wid = _upload(world["hal"], narrative_review_id=str(review.pk), run_id=review.run_id)["id"]
    Feedback.objects.create(target_kind="narrative", target_ref="supply", target_version=2,
                            body="love scene 3", author_email="sophie@x.org", workspace=world["ws"])
    Feedback.objects.create(target_kind="narrative", target_ref="supply", target_version=1,
                            body="old", author_email="sophie@x.org", workspace=world["ws"])
    row = next(r for r in _list(world["human"], "/api/w/connect/walkthroughs/") if r["id"] == wid)
    assert row["comment_count"] == 1


def test_a_view_is_one_visit_per_window_not_one_write_per_poll(world):
    wid = _upload(world["hal"])["id"]
    with override_settings(**DRIVE):
        for _ in range(3):
            world["human"].get(f"/api/w/connect/walkthroughs/{wid}/")
    v = ArtifactView.objects.get(target_kind="walkthrough", target_ref=wid)
    assert v.view_count == 1 and v.user == world["jj"]


def test_anonymous_token_reads_record_nothing(world):
    body = _upload(world["hal"])
    token = body["share_url"].split("t=")[1]
    with override_settings(**DRIVE):
        resp = Client().get(f"/api/walkthroughs/{body['id']}/", {"t": token})
    assert resp.status_code == 200
    assert not ArtifactView.objects.exists()


# ------------------------------------------------------------- narratives


def _open_review(client, *, headers=None, **extra):
    resp = client.post(
        "/api/w/connect/reviews/",
        data=json.dumps({"request_json": {"run_id": "supply-2026-10-09-001", "gate": "concept_change",
                                          "narrative_slug": "supply", "narrative": "story",
                                          "project_slug": "connect-labs"},
                         **extra}),
        content_type="application/json", **(headers or {}))
    assert resp.status_code == 201, resp.content
    return resp.json()["id"]


def test_review_create_stamps_project_slug_and_origin(world):
    rid = _open_review(world["hal"], headers=world["parent"])
    r = ReviewRequest.objects.get(pk=rid)
    assert r.project_slug == "connect-labs"
    assert (r.source_turn_id, r.source_session_id, r.agent_project_id) == (
        world["turn"].pk, world["session"].pk, world["project"].pk)

    rows = _list(world["human"], "/api/w/connect/reviews/", agent_project="hal/P5")
    assert [x["id"] for x in rows] == [rid]
    assert rows[0]["project_slug"] == "connect-labs"
    assert _list(world["human"], "/api/w/connect/reviews/", project="nope") == []
    assert len(_list(world["human"], "/api/w/connect/reviews/", session=str(world["session"].pk))) == 1


def test_narrative_list_carries_project_origin_and_reaction(world):
    rid = _open_review(world["hal"], headers=world["parent"])
    Feedback.objects.create(target_kind="narrative", target_ref="supply", body="yes",
                            author_email="sophie@x.org", workspace=world["ws"])
    # Jonathan opens the review page: a view of the narrative.
    assert world["human"].get(f"/api/w/connect/reviews/{rid}/").status_code == 200

    [n] = _list(world["human"], "/api/w/connect/ddd/narratives/")
    assert n["project_slug"] == "connect-labs"
    assert n["agent_project"]["ext_id"] == "P5"
    assert n["session_id"] == str(world["session"].pk)
    assert (n["comment_count"], n["commenter_count"], n["viewer_count"]) == (1, 1, 1)
    assert n["owner_viewed_at"] is not None
    assert _list(world["human"], "/api/w/connect/ddd/narratives/", agent_project="hal/P9") == []
    assert len(_list(world["human"], "/api/w/connect/ddd/narratives/",
                     session=str(world["session"].pk))) == 1


def test_narrative_project_falls_back_to_the_run_doc_subject(world):
    ReviewRequest.objects.create(run_id="supply-2026-10-09-001", narrative_slug="supply", version=1,
                                 gate="concept_change", request_json={"narrative": "s"},
                                 owner=world["hal_user"], workspace=world["ws"])
    AgentRun.objects.create(agent=world["agent"], kind="ddd", ext_id="supply-2026-10-09-001",
                            subject="supply", project=world["project"])
    [n] = _list(world["human"], "/api/w/connect/ddd/narratives/")
    assert n["agent_project"]["ext_id"] == "P5"


# ------------------------------------------------------------- storyboards


def test_storyboard_create_stamps_origin_and_the_list_shows_reaction(world):
    resp = world["hal"].post(
        "/api/w/connect/storyboards/", data=json.dumps({"slug": "arc", "title": "Arc"}),
        content_type="application/json", **world["parent"])
    assert resp.status_code == 200, resp.content
    board = Storyboard.objects.get(slug="arc")
    assert board.agent_project_id == world["project"].pk
    assert world["human"].get("/api/w/connect/storyboards/arc").status_code == 200
    Feedback.objects.create(target_kind="storyboard", target_ref="arc", body="ok",
                            author_name="Ellyn", workspace=world["ws"])
    [row] = _list(world["human"], "/api/w/connect/storyboards/", agent_project="hal/P5")["items"]
    assert row["turn_id"] == str(world["turn"].pk)
    assert (row["comment_count"], row["viewer_count"]) == (1, 1)
    assert row["owner_viewed_at"] is not None  # hal made it; Jonathan owns hal


# --------------------------------------------------------------- summarize


def test_summarize_is_constant_queries(world, django_assert_max_num_queries):
    targets = [reactions.Target(key=str(i), feedback=[("walkthrough", str(i), None)],
                                views=[("walkthrough", str(i))], creator_id=world["hal_user"].pk)
               for i in range(50)]
    with django_assert_max_num_queries(3):
        out = reactions.summarize(targets)
    assert len(out) == 50


# ----------------------------------------------------------------- backfill


def test_backfill_links_only_unambiguous_projects(world):
    other = AgentProject.objects.create(agent=world["agent"], ext_id="P6", name="other")
    AgentRun.objects.create(agent=world["agent"], kind="ddd", ext_id="supply-2026-10-01-001",
                            subject="supply", project=world["project"])
    AgentRun.objects.create(agent=world["agent"], kind="ddd", ext_id="mixed-2026-10-01-001",
                            subject="mixed", project=world["project"])
    AgentRun.objects.create(agent=world["agent"], kind="ddd", ext_id="mixed-2026-10-02-001",
                            subject="mixed", project=other)
    base = dict(owner=world["hal_user"], workspace=world["ws"], kind="html", drive_file_id="f",
                drive_folder_id="d", content_type="text/html", size_bytes=1)
    by_run = Walkthrough.objects.create(title="a", run_id="supply-2026-10-01-001", **base)
    by_subject = Walkthrough.objects.create(title="b", run_id="supply-2026-10-05-009",
                                            narrative_slug="supply", **base)
    ambiguous = Walkthrough.objects.create(title="c", run_id="mixed-2026-10-09-001",
                                           narrative_slug="mixed", **base)
    review = ReviewRequest.objects.create(run_id="supply-2026-10-05-009", narrative_slug="supply",
                                          gate="concept_change", request_json={},
                                          workspace=world["ws"])

    call_command("backfill_artifact_projects")  # dry run by default
    assert not Walkthrough.objects.filter(agent_project__isnull=False).exists()

    call_command("backfill_artifact_projects", "--apply")
    for obj in (by_run, by_subject, ambiguous, review):
        obj.refresh_from_db()
    assert by_run.agent_project_id == world["project"].pk
    assert by_subject.agent_project_id == world["project"].pk
    assert review.agent_project_id == world["project"].pk
    assert ambiguous.agent_project_id is None  # two projects claim "mixed": skipped
