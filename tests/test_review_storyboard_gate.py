"""The review page is retired: every narrative review lives on a storyboard.

Covers the auto-board on create, the old page's redirect (with and without the
review's token), a gate row ruling resolving the review with the response_json
the DDD reader expects, the round close doing the same, and the backfill's
idempotency."""
from __future__ import annotations

import io

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client

from apps.agents.models import Agent, AgentProject
from apps.feedback.models import Feedback
from apps.reviews.models import ReviewRequest
from apps.storyboards import review_gate
from apps.storyboards.models import Act, Entry, Storyboard, StoryboardDecision
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

NARRATION = [
    {"scene": 1, "id": "open-the-ledger", "title": "Open the ledger", "text": "Amina opens the ledger.",
     "features": [{"id": "f1", "description": "Ledger list", "verify": "rows show"}]},
    {"scene": 2, "id": "send-the-order", "title": "Send the order", "text": "She sends the order.",
     "features": []},
]


def _request_json(slug="supply-phones-every-adult", run_id="supply-phones-every-adult-2026-10-15-001"):
    return {
        "run_id": run_id,
        "narrative_slug": slug,
        "gate": "concept_change",
        "narration": NARRATION,
        "build_order": ["send-the-order", "open-the-ledger"],
        "decisions": [{
            "id": "narrative-verdict",
            "prompt": "Approve this narrative as the build plan, or send it back to re-draft?",
            "options": ["approve", "redraft"],
            "recommended": "approve",
            "class": "concept_change",
        }],
    }


@pytest.fixture()
def world():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")
    viewer = User.objects.create_user("lily", "lily@dimagi.com", "pw", first_name="Lily")
    outsider = User.objects.create_user("out", "out@example.org", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    other = Workspace.objects.create(slug="elsewhere", display_name="Elsewhere", created_by=outsider)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    WorkspaceMembership.objects.create(workspace=other, user=outsider, role=WorkspaceMembership.OWNER)
    clients = {}
    for name, u in (("owner", jj), ("viewer", viewer), ("outsider", outsider)):
        c = Client()
        c.force_login(u)
        clients[name] = c
    return {"ws": ws, "jj": jj, **clients}


def _create(world, **kw):
    resp = world["owner"].post(
        "/api/w/connect/reviews/", {"request_json": _request_json(**kw), "visibility": "link"},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    return ReviewRequest.objects.get(pk=resp.json()["id"])


# ------------------------------------------------------------- auto-board


def test_create_makes_a_one_narrative_board_with_the_gate_row(world):
    review = _create(world)
    board = Storyboard.objects.get(workspace_id="connect")
    assert board.slug == "supply-phones-every-adult"
    assert board.capability == Storyboard.CAP_COMMENT
    assert board.title == "Supply Phones Every Adult"
    assert list(Entry.objects.filter(act__storyboard=board).values_list("narrative_slug", flat=True)) == [
        "supply-phones-every-adult"]
    row = board.decisions.get()
    assert row.review_id == review.pk
    assert row.options == ["approve", "redraft"] and row.recommended == "approve"
    assert row.question.startswith("Approve this narrative")
    assert row.scope == "supply-phones-every-adult"


def test_create_reuses_a_board_that_already_carries_the_narrative(world):
    board = Storyboard.objects.create(slug="connect-supply-rutf", title="RUTF", workspace=world["ws"])
    act = Act.objects.create(storyboard=board, title="A", position=0)
    Entry.objects.create(act=act, narrative_slug="supply-rutf-stock-at-scale", position=0)
    _create(world, slug="supply-rutf-stock-at-scale", run_id="supply-rutf-stock-at-scale-2026-10-14-001")
    assert Storyboard.objects.count() == 1
    assert board.decisions.filter(review__isnull=False).count() == 1


def test_a_new_version_replaces_the_unruled_gate_row(world):
    first = _create(world)
    second = _create(world)
    board = Storyboard.objects.get()
    assert list(board.decisions.values_list("review_id", flat=True)) == [second.pk]
    assert first.pk != second.pk


def test_findings_review_gets_no_board(world):
    resp = world["owner"].post(
        "/api/w/connect/reviews/",
        {"request_json": {"run_id": "x-2026-10-01-001", "gate": "product_findings", "clusters": []},
         "visibility": "link"},
        content_type="application/json",
    )
    assert resp.status_code == 201
    assert not Storyboard.objects.exists()


def test_a_publish_neither_drops_nor_trips_on_a_gate_row(world):
    _create(world)
    board = Storyboard.objects.get()
    row = board.decisions.get()
    world["owner"].post(f"/api/w/connect/storyboards/{board.slug}/decisions/{row.key}/edit",
                        {"action": "pick", "value": "redraft"}, content_type="application/json")
    resp = world["owner"].patch(
        f"/api/w/connect/storyboards/{board.slug}",
        {"decisions": [{"id": "who-is-it-for", "question": "Who?", "options": ["funders"],
                        "recommended": "funders"}]},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert board.decisions.filter(key=row.key).exists()
    assert board.decisions.filter(key="who-is-it-for").exists()


# ------------------------------------------------------------- the old link


def test_old_link_with_the_review_token_lands_on_the_board_with_its_token(world):
    review = _create(world)
    resp = Client().get(f"/w/connect/review/{review.pk}/?t={review.share_token}")
    assert resp.status_code == 302
    board = Storyboard.objects.get()
    assert board.share_token
    assert resp["Location"] == (
        f"/w/connect/storyboard/{board.slug}?t={board.share_token}#entry-supply-phones-every-adult")
    # …and that token really opens the board to a signed-out reader.
    assert Client().get(f"/api/storyboards/{board.slug}?ws=connect&t={board.share_token}").status_code == 200


def test_old_link_with_a_wrong_token_hands_out_no_board_token(world):
    review = _create(world)
    resp = Client().get(f"/w/connect/review/{review.pk}/?t=not-the-token")
    assert resp.status_code == 302
    assert resp["Location"].startswith("/accounts/")
    assert "storyboard" not in resp["Location"].split("next=")[0]
    resp = world["outsider"].get(f"/w/connect/review/{review.pk}/?t=not-the-token")
    assert resp.status_code == 404


def test_old_link_without_a_token_takes_a_member_to_the_board_tokenless(world):
    review = _create(world)
    resp = world["viewer"].get(f"/w/connect/review/{review.pk}/")
    assert resp.status_code == 302
    assert resp["Location"] == "/w/connect/storyboard/supply-phones-every-adult#entry-supply-phones-every-adult"
    assert "t=" not in resp["Location"]


def test_old_link_without_a_token_sends_a_stranger_to_sign_in(world):
    review = _create(world)
    resp = Client().get(f"/w/connect/review/{review.pk}")
    assert resp.status_code == 302 and resp["Location"].startswith("/accounts/google/login/")
    assert world["outsider"].get(f"/w/connect/review/{review.pk}/").status_code == 404


def test_old_link_in_the_wrong_workspace_404s(world):
    review = _create(world)
    assert Client().get(f"/w/elsewhere/review/{review.pk}/?t={review.share_token}").status_code == 404


def test_old_link_makes_the_board_when_the_review_predates_it(world):
    review = ReviewRequest.objects.create(
        run_id="supply-test-kits-2026-09-24-001", narrative_slug="supply-test-kits", gate="concept_change",
        request_json=_request_json("supply-test-kits"), visibility="link", workspace=world["ws"],
    )
    token = review.ensure_share_token()
    resp = Client().get(f"/w/connect/review/{review.pk}/?t={token}")
    assert resp.status_code == 302 and "/storyboard/supply-test-kits?t=" in resp["Location"]


def test_destination_api_matches_the_redirect(world):
    review = _create(world)
    body = Client().get(f"/api/reviews/{review.pk}/destination?t={review.share_token}").json()
    board = Storyboard.objects.get()
    assert body["url"] == f"/w/connect/storyboard/{board.slug}?t={board.share_token}#entry-supply-phones-every-adult"
    assert Client().get(f"/api/reviews/{review.pk}/destination").status_code == 404
    assert world["viewer"].get(f"/api/reviews/{review.pk}/destination").json()["url"].endswith(
        "storyboard/supply-phones-every-adult#entry-supply-phones-every-adult")


# ------------------------------------------------------------- ruling resolves


def _expected_response(value, note=""):
    return {
        "decisions": {"narrative-verdict": value},
        "edited_scenes": [
            {"id": "open-the-ledger", "title": "Open the ledger", "narration": "Amina opens the ledger.",
             "deleted": False, "features": [{"id": "f1", "description": "Ledger list", "verify": "rows show",
                                             "feedback": ""}], "feedback": ""},
            {"id": "send-the-order", "title": "Send the order", "narration": "She sends the order.",
             "deleted": False, "features": [], "feedback": ""},
        ],
        "overall_feedback": note,
        "build_order": ["send-the-order", "open-the-ledger"],
    }


def test_a_member_ruling_the_gate_row_resolves_the_review(world):
    review = _create(world)
    board = Storyboard.objects.get()
    row = board.decisions.get()
    resp = world["owner"].post(f"/api/w/connect/storyboards/{board.slug}/decisions/{row.key}/edit",
                               {"action": "confirm"}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    review.refresh_from_db()
    assert review.status == "resolved" and review.resolved_at is not None
    rj = review.response_json
    via = rj.pop("resolved_via")
    assert via == {"storyboard": board.slug, "decided_by": ["Jonathan"]}
    assert rj == _expected_response("approve")
    # What the DDD poller reads: status resolved, then response_json.decisions.
    polled = world["owner"].get(f"/api/reviews/{review.pk}/").json()
    assert polled["status"] == "resolved"
    assert polled["response_json"]["decisions"] == {"narrative-verdict": "approve"}


def test_redraft_with_reasoning_carries_it_as_overall_feedback(world):
    review = _create(world)
    board = Storyboard.objects.get()
    row = board.decisions.get()
    world["owner"].post(f"/api/w/connect/storyboards/{board.slug}/decisions/{row.key}/edit",
                        {"action": "pick", "value": "redraft", "reasoning": "Scene two is thin."},
                        content_type="application/json")
    review.refresh_from_db()
    review.response_json.pop("resolved_via")
    assert review.response_json == _expected_response("redraft", "Scene two is thin.")


def test_a_viewer_cannot_rule_a_gate_row(world):
    review = _create(world)
    board = Storyboard.objects.get()
    row = board.decisions.get()
    resp = world["viewer"].post(f"/api/w/connect/storyboards/{board.slug}/decisions/{row.key}/edit",
                                {"action": "confirm"}, content_type="application/json")
    assert resp.status_code == 403
    review.refresh_from_db()
    assert review.status == "pending"


def test_a_round_close_with_the_gate_ruled_resolves_the_review(world):
    review = _create(world)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=world["ws"], owner=world["jj"],
                                 user=world["jj"])
    board = Storyboard.objects.get()
    board.agent_project = AgentProject.objects.create(agent=agent, ext_id="P1", name="Phones")
    board.capability = Storyboard.CAP_SUGGEST
    board.save()
    row = board.decisions.get()
    assert world["owner"].post(f"/api/w/connect/storyboards/{board.slug}/rounds").status_code == 200
    staged = world["owner"].post(f"/api/w/connect/storyboards/{board.slug}/decisions/{row.key}/edit",
                                 {"action": "pick", "value": "redraft"}, content_type="application/json")
    assert staged.status_code == 200
    review.refresh_from_db()
    assert review.status == "pending"  # staged, not applied
    closed = world["owner"].post(f"/api/w/connect/storyboards/{board.slug}/rounds/1/close",
                                 {"close_note": ""}, content_type="application/json")
    assert closed.status_code == 200, closed.content
    review.refresh_from_db()
    assert review.status == "resolved"
    assert review.response_json["decisions"] == {"narrative-verdict": "redraft"}


def test_submitting_through_the_api_rules_the_gate_row(world):
    review = _create(world)
    resp = world["owner"].post(f"/api/reviews/{review.pk}/submit/",
                               {"response_json": {"decisions": {"narrative-verdict": "approve"}}},
                               content_type="application/json")
    assert resp.status_code == 200
    row = StoryboardDecision.objects.get(review=review)
    assert row.status == "human-decided" and row.confirmed


def test_suggestion_still_lands_in_the_board_pool(world):
    review = _create(world)
    resp = Client().post(
        f"/api/reviews/{review.pk}/suggest/?t={review.share_token}",
        {"response_json": {"overall_feedback": "Name the phone model."}, "name": "Beth"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert Feedback.objects.filter(target_ref="supply-phones-every-adult", body="Name the phone model.").exists()


# ------------------------------------------------------------- backfill


def test_backfill_is_idempotent_and_skips_boarded_narratives(world):
    rutf = Storyboard.objects.create(slug="connect-supply-rutf", title="RUTF", workspace=world["ws"])
    act = Act.objects.create(storyboard=rutf, title="A", position=0)
    Entry.objects.create(act=act, narrative_slug="supply-rutf-stock-at-scale", position=0)
    for slug, version in (("supply-phones-every-adult", 1), ("supply-phones-every-adult", 2),
                          ("supply-rutf-stock-at-scale", 1)):
        ReviewRequest.objects.create(
            run_id=f"{slug}-2026-10-15-00{version}", narrative_slug=slug, version=version,
            gate="concept_change", request_json=_request_json(slug), visibility="link", workspace=world["ws"],
        )
    ReviewRequest.objects.create(run_id="done-2026-10-01-001", narrative_slug="done", gate="concept_change",
                                 status="resolved", request_json=_request_json("done"), workspace=world["ws"])

    call_command("backfill_review_storyboards", "--workspace", "connect", stdout=io.StringIO())
    assert set(Storyboard.objects.values_list("slug", flat=True)) == {
        "connect-supply-rutf", "supply-phones-every-adult"}
    phones = Storyboard.objects.get(slug="supply-phones-every-adult")
    newest = ReviewRequest.objects.get(narrative_slug="supply-phones-every-adult", version=2)
    assert list(phones.decisions.values_list("review_id", flat=True)) == [newest.pk]
    assert rutf.decisions.count() == 1
    before = list(StoryboardDecision.objects.order_by("pk").values_list("pk", "key"))

    out = io.StringIO()
    call_command("backfill_review_storyboards", "--workspace", "connect", stdout=out)
    assert Storyboard.objects.count() == 2
    assert list(StoryboardDecision.objects.order_by("pk").values_list("pk", "key")) == before
    assert "0 created" in out.getvalue()
    assert ReviewRequest.objects.filter(status="pending").count() == 3


def test_gate_key_is_a_valid_row_id():
    review = ReviewRequest(run_id="r", gate="concept_change", request_json={})
    key = review_gate.gate_key(review, "narrative_verdict")
    assert key.startswith("gate-") and "_" not in key
