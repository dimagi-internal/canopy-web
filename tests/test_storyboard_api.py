"""/api/storyboards — the token gate is the part that matters."""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.feedback.models import Feedback
from apps.storyboards.models import Act, Entry, Storyboard
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def outsider():
    return User.objects.create_user("nope", "nope@example.org", "pw")


@pytest.fixture()
def ws(owner):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    return ws


@pytest.fixture()
def board(ws):
    b = Storyboard.objects.create(
        slug="ecf-supply", title="What the money bought", workspace=ws
    )
    act = Act.objects.create(storyboard=b, title="Act one", position=0)
    Entry.objects.create(act=act, narrative_slug="verified-monitoring", position=0)
    return b


@pytest.fixture()
def member(owner):
    c = Client()
    c.force_login(owner)
    return c


def _post(client, url, payload):
    return client.post(url, json.dumps(payload), content_type="application/json")


COMMENT = {"body": "This act is the one that matters.", "author_name": "Ellyn"}
SUGGESTION = {
    "kind": "suggestion",
    "narrative_slug": "verified-monitoring",
    "suggested_text": "…a re-visit by a QC enumerator.",
    "author_name": "Sophie",
}


# ------------------------------------------------------------------- read gate


def test_anonymous_without_a_token_404s(board):
    assert Client().get(f"/api/storyboards/{board.slug}").status_code == 404


def test_anonymous_with_a_wrong_token_404s_not_403(board):
    """404, never 403 — a 403 would confirm the board exists."""
    board.ensure_share_token()
    r = Client().get(f"/api/storyboards/{board.slug}?t=nope")
    assert r.status_code == 404


def test_a_nonexistent_board_and_a_wrong_token_are_indistinguishable(board):
    board.ensure_share_token()
    missing = Client().get("/api/storyboards/no-such-board")
    wrong = Client().get(f"/api/storyboards/{board.slug}?t=nope")
    assert missing.status_code == wrong.status_code == 404


def test_anonymous_with_the_right_token_reads(board):
    t = board.ensure_share_token()
    r = Client().get(f"/api/storyboards/{board.slug}?t={t}")
    assert r.status_code == 200
    assert r.json()["title"] == "What the money bought"


def test_rotating_the_token_kills_the_old_link(board):
    old = board.ensure_share_token()
    board.rotate_share_token()
    assert Client().get(f"/api/storyboards/{board.slug}?t={old}").status_code == 404


def test_a_member_reads_without_any_token(member, board):
    assert member.get(f"/api/storyboards/{board.slug}").status_code == 200


def test_a_non_member_cannot_read_another_tenants_board(outsider, board):
    c = Client()
    c.force_login(outsider)
    assert c.get(f"/api/storyboards/{board.slug}").status_code == 404


# --------------------------------------------------------------- capabilities


def test_a_read_only_link_cannot_comment(board):
    t = board.ensure_share_token()  # capability defaults to read
    r = _post(Client(), f"/api/storyboards/{board.slug}/feedback?t={t}", COMMENT)
    assert r.status_code == 403
    assert Feedback.objects.count() == 0


def test_a_comment_link_can_comment_but_not_suggest(board):
    board.capability = Storyboard.CAP_COMMENT
    board.save()
    t = board.ensure_share_token()

    assert _post(Client(), f"/api/storyboards/{board.slug}/feedback?t={t}", COMMENT).status_code == 200
    assert _post(Client(), f"/api/storyboards/{board.slug}/feedback?t={t}", SUGGESTION).status_code == 403
    assert Feedback.objects.count() == 1


def test_a_suggest_link_can_do_both(board):
    board.capability = Storyboard.CAP_SUGGEST
    board.save()
    t = board.ensure_share_token()

    assert _post(Client(), f"/api/storyboards/{board.slug}/feedback?t={t}", COMMENT).status_code == 200
    assert _post(Client(), f"/api/storyboards/{board.slug}/feedback?t={t}", SUGGESTION).status_code == 200
    assert Feedback.objects.count() == 2


def test_feedback_without_a_token_404s_before_it_can_403(board):
    """No token means no read access, so the board is invisible — you must not
    learn its capability by probing."""
    assert _post(Client(), f"/api/storyboards/{board.slug}/feedback", COMMENT).status_code == 404


# ------------------------------------------------------------- what gets stored


def test_an_anonymous_comment_records_no_submitted_by(board):
    board.capability = Storyboard.CAP_COMMENT
    board.save()
    t = board.ensure_share_token()
    _post(Client(), f"/api/storyboards/{board.slug}/feedback?t={t}", COMMENT)

    fb = Feedback.objects.get()
    assert fb.author_name == "Ellyn"
    assert fb.submitted_by is None, "the external author has no account to borrow"
    assert fb.channel == "web"


def test_whole_board_feedback_targets_the_storyboard(board):
    board.capability = Storyboard.CAP_COMMENT
    board.save()
    t = board.ensure_share_token()
    _post(Client(), f"/api/storyboards/{board.slug}/feedback?t={t}", COMMENT)

    fb = Feedback.objects.get()
    assert (fb.target_kind, fb.target_ref) == ("storyboard", board.slug)


def test_the_version_it_was_left_against_is_recorded(board):
    """The page FOLLOWS the current release, so without this a comment loses its
    anchor the moment the narrative moves."""
    board.capability = Storyboard.CAP_SUGGEST
    board.save()
    t = board.ensure_share_token()
    _post(
        Client(),
        f"/api/storyboards/{board.slug}/feedback?t={t}",
        {**SUGGESTION, "target_version": 17, "anchor_id": "the-goal"},
    )

    fb = Feedback.objects.get()
    assert (fb.target_kind, fb.target_ref) == ("narrative", "verified-monitoring")
    assert (fb.target_version, fb.anchor_id) == (17, "the-goal")


def test_feedback_against_a_narrative_not_on_this_board_404s(board):
    """An outsider holding one board's link must not be able to file against a
    narrative that board does not contain."""
    board.capability = Storyboard.CAP_COMMENT
    board.save()
    t = board.ensure_share_token()
    r = _post(
        Client(),
        f"/api/storyboards/{board.slug}/feedback?t={t}",
        {**COMMENT, "narrative_slug": "somebody-elses-narrative"},
    )
    assert r.status_code == 404
    assert Feedback.objects.count() == 0


def test_the_caller_cannot_forge_its_channel(board):
    """`channel` is server-set. StrictModel rejects the extra key outright."""
    board.capability = Storyboard.CAP_COMMENT
    board.save()
    t = board.ensure_share_token()
    r = _post(
        Client(),
        f"/api/storyboards/{board.slug}/feedback?t={t}",
        {**COMMENT, "channel": "email"},
    )
    assert r.status_code == 422


# ----------------------------------------------------------------- member ops


def test_create_list_and_share(member, ws):
    created = _post(
        member,
        "/api/storyboards/",
        {
            "slug": "supply",
            "title": "Supply",
            "acts": [{"title": "One", "entries": [{"narrative_slug": "verified-monitoring"}]}],
        },
    )
    assert created.status_code == 200, created.content
    assert created.json()["acts"][0]["entries"][0]["narrative_slug"] == "verified-monitoring"

    listed = member.get("/api/storyboards/").json()["items"]
    assert {b["slug"] for b in listed} == {"supply"}
    assert listed[0]["share_url"] is None, "no token minted until you ask"

    share = member.post("/api/storyboards/supply/share").json()
    assert "?t=" in share["share_url"]


def test_patch_replaces_the_acts_wholesale(member, board):
    r = member.patch(
        f"/api/storyboards/{board.slug}",
        json.dumps({"title": "Renamed", "acts": [{"title": "Only act", "entries": []}]}),
        content_type="application/json",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Renamed"
    assert [a["title"] for a in body["acts"]] == ["Only act"]


def test_an_outsider_cannot_edit(outsider, board):
    c = Client()
    c.force_login(outsider)
    r = c.patch(
        f"/api/storyboards/{board.slug}",
        json.dumps({"title": "Mine now"}),
        content_type="application/json",
    )
    assert r.status_code == 404


# --------------------------------------------------- the reviewer surface read


def test_reading_a_narrative_uses_the_same_token_gate(board):
    t = board.ensure_share_token()
    url = f"/api/storyboards/{board.slug}/narratives/verified-monitoring"
    assert Client().get(url).status_code == 404
    assert Client().get(f"{url}?t=nope").status_code == 404
    # right token, but nothing published yet for that slug
    assert Client().get(f"{url}?t={t}").status_code == 404


def test_a_narrative_not_on_this_board_404s(board, ws):
    """One board's link must not be a read handle for every narrative in the
    workspace."""
    from apps.reviews.models import ReviewRequest

    ReviewRequest.objects.create(
        run_id="somebody-elses-2026-07-26-001",
        request_json={
            "gate": "concept_change",
            "narrative_slug": "somebody-elses",
            "narration": [{"id": "x", "title": "X", "text": "Secret."}],
        },
        gate="concept_change",
        visibility="link",
        workspace_id=ws.slug,
        version=1,
    )
    t = board.ensure_share_token()
    r = Client().get(f"/api/storyboards/{board.slug}/narratives/somebody-elses?t={t}")
    assert r.status_code == 404


def test_the_reader_gets_current_and_previous_narration(board, ws):
    from apps.reviews.models import ReviewRequest

    for version, text in ((1, "The old line."), (2, "The new line.")):
        ReviewRequest.objects.create(
            run_id=f"verified-monitoring-2026-07-26-00{version}",
            request_json={
                "gate": "concept_change",
                "narrative_slug": "verified-monitoring",
                "narration": [{"id": "the-goal", "title": "The goal", "text": text}],
            },
            gate="concept_change",
            visibility="link",
            workspace_id=ws.slug,
            version=version,
        )

    t = board.ensure_share_token()
    r = Client().get(f"/api/storyboards/{board.slug}/narratives/verified-monitoring?t={t}")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["version"] == 2
    assert body["previous_version"] == 1
    assert body["narration"][0]["text"] == "The new line."
    assert body["previous_narration"][0]["text"] == "The old line."
    assert body["capability"] == "read"


def test_the_share_url_carries_the_deployment_script_prefix(member, board, settings):
    """labs serves under FORCE_SCRIPT_NAME=/canopy. build_absolute_uri on a
    leading-slash path DROPS it, which mints a link that 404s — this shipped
    broken and was only caught by opening the link. apps/walkthroughs already
    solved it with get_script_prefix(); this is the same fix."""
    from django.urls import set_script_prefix

    set_script_prefix("/canopy/")
    try:
        url = member.post(f"/api/storyboards/{board.slug}/share").json()["share_url"]
        assert "/canopy/storyboard/" in url, url
        assert "?t=" in url
    finally:
        set_script_prefix("/")


def test_the_share_url_has_no_prefix_when_served_at_root(member, board):
    from django.urls import set_script_prefix

    set_script_prefix("/")
    url = member.post(f"/api/storyboards/{board.slug}/share").json()["share_url"]
    assert "/storyboard/" in url and "/canopy/" not in url, url
