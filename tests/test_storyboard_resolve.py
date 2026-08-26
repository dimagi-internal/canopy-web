"""Resolving a storyboard: follow the current release, never 500 on an unbuilt act."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.reviews.models import ReviewRequest
from apps.storyboards.models import Act, Entry, Storyboard
from apps.storyboards.services import resolve_board
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    return Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)


@pytest.fixture()
def board(ws):
    b = Storyboard.objects.create(
        slug="ecf-supply", title="What the money bought", lede="Three acts.", workspace=ws
    )
    act = Act.objects.create(storyboard=b, title="Act one", prose="Why this first.", position=0)
    Entry.objects.create(act=act, narrative_slug="verified-monitoring", position=0)
    return b


def _publish(ws, slug: str, version: int, title: str, story: str) -> ReviewRequest:
    """A narrative version, the way canopy-web actually stores one."""
    return ReviewRequest.objects.create(
        run_id=f"{slug}-2026-07-26-00{version}",
        request_json={
            "gate": "concept_change",
            "narrative_slug": slug,
            "narration": [{"id": "the-goal", "title": title, "text": story}],
            "narrative": story,
        },
        gate="concept_change",
        visibility="link",
        workspace_id=ws.slug,
        version=version,
    )


def test_the_arc_keeps_its_shape(board):
    out = resolve_board(board)
    assert out["title"] == "What the money bought"
    assert out["lede"] == "Three acts."
    assert [a["title"] for a in out["acts"]] == ["Act one"]
    assert out["acts"][0]["prose"] == "Why this first."


def test_an_unpublished_narrative_resolves_to_a_placeholder_not_an_error(board):
    """A storyboard is usually authored BEFORE its narratives are rendered. An
    arc that 500s because act three has not been filmed yet would be useless
    exactly when you are building it."""
    entry = resolve_board(board)["acts"][0]["entries"][0]
    assert entry["narrative_slug"] == "verified-monitoring"
    assert entry["published"] is False
    assert entry["video_url"] is None


def test_an_entry_follows_the_current_version(board, ws):
    _publish(ws, "verified-monitoring", 1, "First cut", "The old story.")
    entry = resolve_board(board)["acts"][0]["entries"][0]
    assert entry["published"] is True
    assert entry["version"] == 1

    _publish(ws, "verified-monitoring", 2, "Sophie's revision", "The new story.")
    entry = resolve_board(board)["acts"][0]["entries"][0]
    assert entry["version"] == 2, "the page must FOLLOW, not freeze"
    assert "new story" in entry["lede"]


def test_entries_resolve_in_position_order(board, ws):
    act = board.acts.first()
    Entry.objects.create(act=act, narrative_slug="microplans-study-groups", position=-1)
    slugs = [e["narrative_slug"] for e in resolve_board(board)["acts"][0]["entries"]]
    assert slugs == ["microplans-study-groups", "verified-monitoring"]


def test_another_tenants_narrative_does_not_resolve(board, owner):
    """Scoping is the board's workspace — a slug collision in another tenant
    must not leak that tenant's story onto this board."""
    other = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    _publish(other, "verified-monitoring", 9, "Someone else's", "Not yours.")
    entry = resolve_board(board)["acts"][0]["entries"][0]
    assert entry["published"] is False


def test_capability_is_carried_so_the_page_knows_what_to_offer(board):
    assert resolve_board(board)["capability"] == Storyboard.CAP_READ
    board.capability = Storyboard.CAP_SUGGEST
    board.save()
    assert resolve_board(board)["capability"] == Storyboard.CAP_SUGGEST


def test_a_card_never_repeats_its_lede_as_its_heading(board, ws):
    """canopy-web derives a narrative's `title` from the opening of its story,
    so using it verbatim made every card show the same paragraph twice — once
    truncated as the heading, once in full beneath it. Observed on all three
    live RF Surveys narratives."""
    long_story = (
        "Maya's goal is to measure differences in outcomes between her program's "
        "intervention areas and carefully matched non-intervention areas — a rigorous "
        "matched comparison, not a randomized trial. She wants to leverage the Connect "
        "network to carry it out."
    )
    _publish(ws, "verified-monitoring", 1, long_story, long_story)

    entry = resolve_board(board)["acts"][0]["entries"][0]
    assert entry["title"] == "Verified Monitoring"
    assert not entry["lede"].startswith(entry["title"])
    assert len(entry["title"]) <= 70


def test_the_lede_is_one_sentence_not_the_whole_story(board, ws):
    story = "First sentence here. Second sentence that should not appear on the card."
    _publish(ws, "verified-monitoring", 1, "A title", story)

    entry = resolve_board(board)["acts"][0]["entries"][0]
    assert entry["lede"] == "First sentence here."


def test_a_genuinely_short_title_is_still_used(board, ws):
    """canopy-web ALWAYS derives a narrative's title from the opening of its
    story — there is no separate title field to pass. So a short title only
    exists when the story itself opens with a short line, and in that case it is
    a real title and should win over the humanized slug."""
    _publish(ws, "verified-monitoring", 1, "ignored", "Checking the checkers.")
    entry = resolve_board(board)["acts"][0]["entries"][0]
    assert entry["title"] == "Checking the checkers."


def _film(ws, owner, review, visibility: str):
    """The walkthrough that IS this narrative version's demo."""
    from apps.walkthroughs.models import Walkthrough

    w = Walkthrough.objects.create(
        title="Demo",
        kind=Walkthrough.KIND_VIDEO,
        narrative_review_id=review.id,
        visibility=visibility,
        workspace_id=ws.slug,
        owner=owner,
        drive_file_id="f",
        drive_folder_id="d",
        content_type="video/mp4",
        size_bytes=1,
    )
    if visibility == Walkthrough.VISIBILITY_LINK:
        w.ensure_share_token()
    return w


def test_a_reader_is_not_handed_a_video_they_cannot_stream(board, ws, owner):
    """The board FOLLOWS the current release, so the next version's walkthrough
    can land private under a link that has already been sent. A <video> pointing
    at a stream that 404s renders a black box with no explanation."""
    review = _publish(ws, "verified-monitoring", 1, "Cut", "The story.")
    _film(ws, owner, review, "private")

    entry = resolve_board(board, is_member=False)["acts"][0]["entries"][0]
    assert entry["published"] is True, "the narrative IS published; only its film is not shared"
    assert entry["video_url"] is None

    mine = resolve_board(board, is_member=True)["acts"][0]["entries"][0]
    assert mine["video_url"], "a member streams a private artifact through their session"


def test_a_public_film_still_reaches_the_reader(board, ws, owner):
    review = _publish(ws, "verified-monitoring", 1, "Cut", "The story.")
    _film(ws, owner, review, "link")

    entry = resolve_board(board, is_member=False)["acts"][0]["entries"][0]
    assert "t=" in entry["video_url"], "an anonymous stream needs the artifact's own token"


def test_layout_is_carried_so_the_page_knows_how_much_to_show(board):
    assert resolve_board(board)["layout"] == Storyboard.LAYOUT_REVIEW
    board.layout = Storyboard.LAYOUT_REEL
    board.save()
    assert resolve_board(board)["layout"] == Storyboard.LAYOUT_REEL


def test_an_authored_title_and_blurb_beat_the_derived_ones(board, ws):
    """A derived heading is a humanised slug and a derived one-liner is the
    story's opening sentence — written to carry a reader INTO a narrative, not
    to state flatly what a video shows. A reel needs the latter."""
    _publish(ws, "verified-monitoring", 1, "Cut one", "Maya opens the dashboard and wonders.")
    entry = board.acts.first().entries.first()
    entry.title = "Survey quality review"
    entry.blurb = "Six rounds of independent survey data, with one surveyor flagged."
    entry.save()

    out = resolve_board(board)["acts"][0]["entries"][0]
    assert out["title"] == "Survey quality review"
    assert out["lede"] == "Six rounds of independent survey data, with one surveyor flagged."


def test_an_authored_title_survives_the_narrative_being_unbuilt(board):
    """The placeholder branch is the one an author looks at while writing the
    board, so it must show what they wrote rather than the slug."""
    entry = board.acts.first().entries.first()
    entry.title = "Survey quality review"
    entry.blurb = "Not filmed yet, but this is what it will show."
    entry.save()

    out = resolve_board(board)["acts"][0]["entries"][0]
    assert out["published"] is False
    assert out["title"] == "Survey quality review"
    assert out["lede"] == "Not filmed yet, but this is what it will show."
