"""Stored flat links are rewritten to the scoped address (canopy-web#1337).

The flat addresses are a plain 404 and nothing forwards them, so the links
already baked into canopy's data — review payloads, task links, the `<video
src>` inside an uploaded deck — are pointed at `/w/<ws>/…` in place. These pin
what that rewrite keeps (host, `/canopy` mount, `?t=`, `#fragment`), what it
refuses to touch (anything it cannot resolve to one workspace), and that a
second run is a no-op.
"""
from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.core.management import call_command

from apps.runs.tests.factories import make_review, make_user, make_walkthrough, make_workspace
from apps.session_sharing.models import Session, ShareToken
from apps.storyboards.models import Storyboard
from apps.walkthroughs import flat_links
from apps.walkthroughs.models import Walkthrough
from tests.fixtures.fake_drive import FakeDriveClient

U = "2f9a1c34-5b6d-4e7f-8a9b-0c1d2e3f4a5b"
R = "7c1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"


KNOWN = {
    ("walkthrough", U): "connect",
    ("review", R): "dimagi",
    ("share", "tok123"): "connect",
    ("storyboard", "oes-supply"): "connect",
    ("ddd-release", "chlorine-2026-10-07-002"): "connect",
}


def _rw(text):
    def resolve(kind, ident, following):
        if kind == "narrative":
            m = flat_links._BOARD_PARAM.match(following)
            return KNOWN.get(("storyboard", m.group(1))) if m else None
        return KNOWN.get((kind, ident))
    return flat_links.rewrite_text(text, resolve)


@pytest.mark.parametrize("before, after", [
    # the content stream, absolute, with its token — the deck <video src> case
    (f"https://canopy.dimagi.com/walkthrough/{U}/content?t=abc",
     f"https://canopy.dimagi.com/w/connect/walkthrough/{U}/content?t=abc"),
    # the labs mount keeps its /canopy prefix and host
    (f"https://labs.connect.dimagi.com/canopy/walkthrough/{U}/content?t=abc",
     f"https://labs.connect.dimagi.com/canopy/w/connect/walkthrough/{U}/content?t=abc"),
    # the viewer, with a fragment
    (f"https://labs.connect.dimagi.com/canopy/walkthrough/{U}?t=abc#scene-3",
     f"https://labs.connect.dimagi.com/canopy/w/connect/walkthrough/{U}?t=abc#scene-3"),
    # the pre-tenancy /w/<uuid> viewer and stream
    (f"https://labs.connect.dimagi.com/canopy/w/{U}/content",
     f"https://labs.connect.dimagi.com/canopy/w/connect/walkthrough/{U}/content"),
    (f"https://canopy.dimagi.com/w/{U}?t=abc", f"https://canopy.dimagi.com/w/connect/walkthrough/{U}?t=abc"),
    # relative, as a review's video.url
    (f"/walkthrough/{U}/content", f"/w/connect/walkthrough/{U}/content"),
    (f"/review/{R}/?t=x", f"/w/dimagi/review/{R}/?t=x"),
    ("https://canopy.dimagi.com/share/tok123", "https://canopy.dimagi.com/w/connect/share/tok123"),
    ("https://canopy.dimagi.com/storyboard/oes-supply?t=s", "https://canopy.dimagi.com/w/connect/storyboard/oes-supply?t=s"),
    ("/narrative/chlorine?b=oes-supply&t=s", "/w/connect/narrative/chlorine?b=oes-supply&t=s"),
    ("https://labs.connect.dimagi.com/canopy/ddd-release/chlorine/chlorine-2026-10-07-002?t=s",
     "https://labs.connect.dimagi.com/canopy/w/connect/ddd-release/chlorine/chlorine-2026-10-07-002?t=s"),
])
def test_a_resolvable_flat_link_moves_under_its_workspace(before, after):
    assert _rw(before) == (after, 1, 0)
    # in prose and markup too
    assert _rw(f'see <a href="{before}">it</a>.')[0] == f'see <a href="{after}">it</a>.'


@pytest.mark.parametrize("text", [
    # already scoped: never re-matched at its inner /walkthrough/
    f"https://canopy.dimagi.com/w/connect/walkthrough/{U}/content?t=abc",
    f"/w/connect/review/{R}/",
    # mid-path, someone else's address or a file path
    "https://labs.connect.dimagi.com/microplans/program/135/group/3342/share/tok123",
    "scripts/walkthrough/generate_presentation.py",
    # a workspace page, not an artifact
    "/w/dimagi/agents",
])
def test_what_is_not_a_flat_artifact_link_is_left_alone(text):
    assert _rw(text) == (text, 0, 0)


@pytest.mark.parametrize("text", [
    # route NAMES in prose, a deleted walkthrough, a narrative with no board
    "Open /review/<id> to approve",
    f"https://canopy.dimagi.com/walkthrough/{R}/content",
    "/narrative/chlorine",
])
def test_an_unresolvable_link_is_counted_not_guessed(text):
    new, done, left = _rw(text)
    assert new == text and done == 0
    assert left == (0 if "<id>" in text else 1)


def test_json_values_are_walked_and_keys_kept():
    doc = {"video": {"url": f"/walkthrough/{U}/content", "poster": None},
           "decisions": [{"prompt": f"watch https://canopy.dimagi.com/walkthrough/{U}?t=a#t=18"}]}
    new, done, left = flat_links.rewrite_json(doc, lambda k, i, f: KNOWN.get((k, i)))
    assert done == 2 and left == 0
    assert new["video"] == {"url": f"/w/connect/walkthrough/{U}/content", "poster": None}
    assert new["decisions"][0]["prompt"].endswith(f"/w/connect/walkthrough/{U}?t=a#t=18")


@pytest.mark.django_db
class TestSweep:
    @pytest.fixture
    def data(self):
        connect, dimagi = make_workspace("connect"), make_workspace("dimagi")
        owner = make_user()
        video = make_walkthrough(owner, workspace=connect, kind="video", run_id="run-1")
        content = f"https://labs.connect.dimagi.com/canopy/walkthrough/{video.id}/content?t=tk"
        review = make_review(
            owner, workspace=dimagi, run_id="run-1",
            request_json={"video": {"url": f"/walkthrough/{video.id}/content"},
                          "deck_url": f"https://canopy-web-x.a.run.app/w/{video.id}",
                          "narration": [{"features": [{"verify": "open /review/ and approve"}]}]},
        )
        session = Session.objects.create(owner=owner, workspace=connect, title="t", visibility="link")
        share = ShareToken.objects.create(session=session, created_by=owner).token
        board = Storyboard.objects.create(slug="arc", title="Arc", workspace=connect)

        from apps.agents import services as agents

        agent = agents.upsert_agent(
            SimpleNamespace(slug="ace", name="ACE", description="", persona="", email="", avatar_url=""),
            workspace=dimagi,
        )
        task = agents.create_tasks(agent, [{
            "title": "Review the chlorine story",
            "links": [{"label": "review", "url": f"https://canopy.dimagi.com/review/{review.id}/?t=x"},
                      {"label": "arc", "url": f"https://canopy.dimagi.com/storyboard/{board.slug}?t=s"}],
            "notes": f"Transcript: https://canopy.dimagi.com/share/{share}",
        }])[0]
        fake = FakeDriveClient()
        folder = fake.find_or_create_folder("deck", fake.root_id)
        html = f'<video src="{content}"></video><a href="https://canopy.dimagi.com/review/{review.id}/">r</a>'
        file_id = fake.upload(parent_id=folder, name="deck.html", content_type="text/html", data=html.encode())
        deck = make_walkthrough(owner, workspace=connect, kind="html", role="deck",
                                drive_file_id=file_id, drive_folder_id=folder, size_bytes=len(html))
        return SimpleNamespace(video=video, review=review, task=task, share=share, deck=deck,
                               fake=fake, old_file=file_id)

    def test_rewrites_rows_and_decks_under_each_artifacts_own_workspace(self, data):
        stats = flat_links.sweep(apps, client=data.fake)
        v = data.video.id

        data.review.refresh_from_db()
        assert data.review.request_json["video"]["url"] == f"/w/connect/walkthrough/{v}/content"
        assert data.review.request_json["deck_url"] == f"https://canopy-web-x.a.run.app/w/connect/walkthrough/{v}"
        # prose naming a route is not a link to anything
        assert data.review.request_json["narration"][0]["features"][0]["verify"] == "open /review/ and approve"

        data.task.refresh_from_db()
        assert data.task.links[0]["url"] == f"https://canopy.dimagi.com/w/dimagi/review/{data.review.id}/?t=x"
        assert data.task.links[1]["url"] == "https://canopy.dimagi.com/w/connect/storyboard/arc?t=s"
        assert data.task.notes == f"Transcript: https://canopy.dimagi.com/w/connect/share/{data.share}"

        deck = Walkthrough.objects.get(pk=data.deck.pk)
        assert deck.drive_file_id != data.old_file
        assert data.old_file not in data.fake.files  # the old file is trashed
        body = data.fake.files[deck.drive_file_id].data.decode()
        assert f"/canopy/w/connect/walkthrough/{v}/content?t=tk" in body
        assert f"/w/dimagi/review/{data.review.id}/" in body
        assert deck.size_bytes == len(body.encode())

        assert stats["rows_rewritten"] == 2
        assert stats["blobs_rewritten"] == 1 and stats["blob_links_rewritten"] == 2

    def test_a_second_run_changes_nothing(self, data):
        flat_links.sweep(apps, client=data.fake)
        deck_file = Walkthrough.objects.get(pk=data.deck.pk).drive_file_id
        again = flat_links.sweep(apps, client=data.fake)
        assert again["links_rewritten"] == 0 and again["blobs_rewritten"] == 0
        assert Walkthrough.objects.get(pk=data.deck.pk).drive_file_id == deck_file

    def test_a_dry_run_counts_and_writes_nothing(self, data):
        before = dict(data.review.request_json)
        stats = flat_links.sweep(apps, dry_run=True, client=data.fake)
        data.review.refresh_from_db()
        assert data.review.request_json == before
        assert Walkthrough.objects.get(pk=data.deck.pk).drive_file_id == data.old_file
        assert stats["links_rewritten"] >= 4 and stats["blobs_rewritten"] == 1

    def test_without_drive_the_rows_still_move(self, data, settings):
        settings.CANOPY_DRIVE_SA_KEY_JSON = ""
        out = StringIO()
        call_command("rewrite_flat_links", stdout=out)
        data.review.refresh_from_db()
        assert data.review.request_json["video"]["url"].startswith("/w/connect/")
        assert "blobs_skipped_no_drive=1" in out.getvalue()
