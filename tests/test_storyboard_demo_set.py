"""A storyboard as the one link for a SET of recorded demos.

Parts 1–3 of the Connect Supply demos needed a companion Google Doc because the
board carried a title and a lede and nothing else: not who the demos are for,
not the point to land, not that every number on screen is synthetic, not the
3-minute version, not the cuts the set is made of, and not where the script and
the data disagree. These pin the board carrying all of it, additively.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client

from apps.feedback.models import Feedback
from apps.reviews.models import ReviewRequest
from apps.runs.tests.factories import make_walkthrough
from apps.storyboards import cuts as entry_cuts
from apps.storyboards.models import Act, Entry, Storyboard
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SLUG = "supply-rutf-stock-at-scale"
NARRATION = [
    {"id": "overview", "scene": 1, "title": "Every procurement in one place",
     "text": "Each line is a tender or an order.", "provenance": "supply_chain overview, programme 10112"},
    {"id": "suppliers", "scene": 2, "title": "The tender's suppliers",
     "text": "We asked four suppliers to quote.", "provenance": "tender_detail, tender 356"},
    {"id": "compare", "scene": 3, "title": "Comparing quotes",
     "text": "Each quote on a landed price.", "provenance": "tender_detail, tender 356"},
]
BRIEF = {
    "audience": "Edesia and OES",
    "purpose": "One set of screens shows where every sachet is.",
    "data_disclosure": "All on-screen data is synthetic. Source: Labs programme 10112.",
    "short_version": {"label": "3-minute version",
                      "items": [{"narrative_slug": SLUG, "cut_id": "procurements"}]},
    "assumptions": ["Programme 10112 stands in for the real programme."],
    "divergences": [{"script": "Four suppliers", "data": "Three suppliers in tender 356",
                     "resolution": "Re-record cut 2"}],
    "open_questions": ["Does OES want the forecast cut?"],
}


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    return ws


@pytest.fixture()
def member(owner):
    c = Client()
    c.force_login(owner)
    return c


@pytest.fixture()
def board(ws):
    b = Storyboard.objects.create(
        slug="supply", title="Connect Supply demos", workspace=ws, brief=BRIEF,
        capability="comment",
    )
    b.ensure_share_token()
    act = Act.objects.create(storyboard=b, title="Part 1", position=0)
    Entry.objects.create(act=act, narrative_slug=SLUG, position=0)
    return b


def _version(ws, owner, *, cuts_meta=None, extra=None):
    rj = {"gate": "concept_change", "narrative_slug": SLUG, "narration": NARRATION,
          "narrative": "A programme that buys therapeutic food sees it all.",
          "title": "Part 1 — RUTF stock at scale", **(extra or {})}
    if cuts_meta is not None:
        rj["cuts"] = cuts_meta
    return ReviewRequest.objects.create(
        run_id=f"{SLUG}-2026-10-14-001", request_json=rj, gate="concept_change",
        visibility="link", workspace_id=ws.slug, version=1, owner=owner,
    )


def _cut(ws, owner, review, cut_id, scene_ids, *, public=True, **kw):
    w = make_walkthrough(
        owner, kind="video", workspace=ws, title=kw.pop("title", cut_id),
        visibility="link" if public else "private", narrative_slug=SLUG,
        narrative_review_id=review.id, cut_id=cut_id, cut_scene_ids=scene_ids, **kw,
    )
    if public:
        w.ensure_share_token()
    return w


def _entry(client, board, token=True):
    url = f"/api/storyboards/{board.slug}" + (f"?t={board.share_token}" if token else "")
    r = client.get(url)
    assert r.status_code == 200, r.content
    return r.json(), r.json()["acts"][0]["entries"][0]


# ------------------------------------------------------------------ the brief


def test_the_brief_round_trips_through_create_and_read(member, ws):
    r = member.post("/api/w/connect/storyboards/", json.dumps({
        "slug": "supply", "title": "Connect Supply demos", "brief": BRIEF, "acts": [],
    }), content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["brief"]["audience"] == "Edesia and OES"
    body = member.get("/api/storyboards/supply?ws=connect").json()
    assert body["brief"]["data_disclosure"].startswith("All on-screen data is synthetic")
    assert body["brief"]["short_version"]["items"] == [{"narrative_slug": SLUG, "cut_id": "procurements"}]
    assert body["brief"]["divergences"][0]["resolution"] == "Re-record cut 2"


def test_a_board_without_a_brief_reads_an_empty_one(member, ws):
    Storyboard.objects.create(slug="plain", title="Plain", workspace=ws)
    brief = member.get("/api/storyboards/plain").json()["brief"]
    assert brief["audience"] == "" and brief["assumptions"] == [] and brief["short_version"] is None


def test_patch_replaces_the_brief_and_leaves_the_arc(member, board):
    r = member.patch(f"/api/storyboards/{board.slug}", json.dumps({"brief": {"audience": "Lily's team"}}),
                     content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["brief"]["audience"] == "Lily's team"
    assert r.json()["brief"]["assumptions"] == []
    assert Entry.objects.filter(act__storyboard=board).count() == 1


def test_a_malformed_brief_is_refused_not_stored(member, board):
    r = member.patch(f"/api/storyboards/{board.slug}", json.dumps({"brief": {"audiance": "typo"}}),
                     content_type="application/json")
    assert r.status_code == 422
    board.refresh_from_db()
    assert board.brief == BRIEF


def test_the_import_command_reads_a_brief(ws, tmp_path):
    f = tmp_path / "board.yaml"
    f.write_text(
        "slug: supply\ntitle: Supply\nbrief:\n  audience: Edesia\n  open_questions: ['Which cut first?']\n"
        "acts:\n  - title: Part 1\n    entries: [supply-rutf-stock-at-scale]\n"
    )
    call_command("import_storyboard", str(f), workspace=ws.slug)
    assert Storyboard.objects.get(slug="supply").brief == {
        "audience": "Edesia", "open_questions": ["Which cut first?"],
    }


# --------------------------------------------------------------- the cuts


def test_a_guest_sees_every_cut_with_the_words_written_about_it(ws, owner, board):
    v = _version(ws, owner, cuts_meta=[
        {"id": "procurements", "screen_notes": "Overview page; cursor on the tender row.",
         "audience_note": "For Edesia: their order is the one on the road."},
        {"id": "buying", "title": "Buying RUTF", "screen_notes": "Tender 356, then compare."},
    ])
    _cut(ws, owner, v, "procurements", ["overview"], title="Every procurement", duration_sec=21)
    _cut(ws, owner, v, "buying", ["suppliers", "compare"], title="Buying")

    _, entry = _entry(Client(), board)
    assert entry["title"] == "Part 1 — RUTF stock at scale", "the publisher's title, not the slug"
    cuts = entry["cuts"]
    assert [c["cut_id"] for c in cuts] == ["procurements", "buying"]
    first, second = cuts
    assert first["anchor_id"] == "cut:procurements"
    assert first["duration_sec"] == 21
    assert first["word_count"] == 8
    assert first["screen_notes"].startswith("Overview page")
    assert first["audience_note"].startswith("For Edesia")
    assert first["provenance"] == "supply_chain overview, programme 10112", "scene provenance by default"
    assert "?t=" in first["video_url"]
    assert second["title"] == "Buying RUTF", "the publisher's cut title wins"
    assert [s["id"] for s in second["scenes"]] == ["suppliers", "compare"]
    assert second["provenance"] == "tender_detail, tender 356", "one source, not repeated"


def test_a_private_cut_is_listed_but_not_handed_to_a_guest(ws, owner, board):
    v = _version(ws, owner)
    _cut(ws, owner, v, "procurements", ["overview"], public=False)
    _, entry = _entry(Client(), board)
    assert entry["cuts"][0]["recorded"] is True
    assert entry["cuts"][0]["video_url"] is None


def test_a_member_gets_the_private_cut(ws, owner, board, member):
    v = _version(ws, owner)
    _cut(ws, owner, v, "procurements", ["overview"], public=False)
    _, entry = _entry(member, board, token=False)
    assert entry["cuts"][0]["video_url"]


def test_a_declared_cut_with_no_video_reads_not_recorded(ws, owner, board):
    v = _version(ws, owner, cuts_meta=[
        {"id": "procurements"},
        {"id": "forecast", "title": "Forecast (optional)", "scene_ids": ["compare"], "duration_sec": 30},
    ])
    _cut(ws, owner, v, "procurements", ["overview"])
    _, entry = _entry(Client(), board)
    forecast = entry["cuts"][1]
    assert forecast["cut_id"] == "forecast"
    assert forecast["recorded"] is False and forecast["video_url"] is None
    assert forecast["optional"] is True
    assert forecast["duration_sec"] == 30


def test_the_publishers_cut_order_is_the_running_order(ws, owner, board):
    v = _version(ws, owner, cuts_meta=[{"id": "buying"}, {"id": "procurements"}])
    _cut(ws, owner, v, "procurements", ["overview"])
    _cut(ws, owner, v, "buying", ["suppliers"])
    _, entry = _entry(Client(), board)
    assert [c["cut_id"] for c in entry["cuts"]] == ["buying", "procurements"]


def test_an_explainer_narrative_has_no_cuts_and_its_caveats_ride_along(ws, owner, board):
    _version(ws, owner, extra={
        "assumptions": ["Stock counts are weekly."],
        "divergences": [{"script": "twenty workers", "data": "nineteen"}, {"data": "no script"}],
        "open_questions": ["Name the partner store?"],
    })
    _, entry = _entry(Client(), board)
    assert entry["cuts"] == []
    assert entry["assumptions"] == ["Stock counts are weekly."]
    assert entry["divergences"] == [
        {"script": "twenty workers", "data": "nineteen", "resolution": "", "narrative_slug": SLUG}
    ]
    assert entry["open_questions"] == ["Name the partner store?"]


def test_an_unpublished_entry_still_reads(board):
    _, entry = _entry(Client(), board)
    assert entry["published"] is False and entry["cuts"] == []


# --------------------------------------------------------- per-cut comments


def test_a_comment_on_a_cut_records_which_cut(ws, owner, board):
    v = _version(ws, owner)
    _cut(ws, owner, v, "procurements", ["overview"])
    r = Client().post(
        f"/api/storyboards/{board.slug}/feedback?t={board.share_token}",
        json.dumps({"narrative_slug": SLUG, "target_version": 1, "anchor_id": "cut:procurements",
                    "body": "Too fast on the tender row.", "author_name": "Lily"}),
        content_type="application/json",
    )
    assert r.status_code == 200, r.content
    fb = Feedback.objects.get()
    assert (fb.target_kind, fb.target_ref, fb.anchor_id) == ("narrative", SLUG, "cut:procurements")


# ------------------------------------------------------------- pure helper


def test_word_count_comes_from_the_narration_not_the_publisher():
    rows = entry_cuts.entry_cuts(
        [{"cut_id": "a", "title": "A", "scene_ids": ["overview"], "video_url": "/x?t=1"}],
        {"narration": NARRATION, "cuts": [{"id": "a", "word_count": 999}]},
        playable=lambda url: True,
    )
    assert rows[0]["word_count"] == 8
