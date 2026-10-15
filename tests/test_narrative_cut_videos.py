"""Per-cut videos on a narrative version (canopy-web#1288).

A ``style: recorded`` narrative renders one mp4 per cut (canopy#796). Each is
uploaded with the recipe's ``cuts[].id`` and the scene ids it plays, and lands
in its own slot on the version: on the narrative page (``build_narrative``) and
on the review link's Cuts tab, for guests as well as members.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.utils import timezone

from apps.runs import aggregate
from apps.runs.tests.factories import make_review, make_user, make_walkthrough
from apps.walkthroughs import storage
from apps.walkthroughs.models import Walkthrough
from tests.fixtures.fake_drive import FakeDriveClient

pytestmark = pytest.mark.django_db

SLUG = "chlorine"
RUN = "chlorine-2026-10-07-002"
NARRATION = [
    {"id": "c1-a", "scene": 1, "title": "Cut 1 · Register — a", "text": "Register a."},
    {"id": "c1-b", "scene": 2, "title": "Cut 1 · Register — b", "text": "Register b."},
    {"id": "c2-a", "scene": 3, "title": "Cut 2 · Decide — a", "text": "Decide a."},
    {"id": "c3-a", "scene": 4, "title": "Cut 3 · Deliver — a", "text": "Deliver a."},
]
_DDD_SETTINGS = dict(
    WALKTHROUGHS_ENABLED=True,
    CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder",
    CANOPY_DRIVE_SA_KEY_JSON='{"x":"y"}',
)


def _version(owner, **kw):
    return make_review(
        owner, run_id=RUN, gate="concept_change", narrative_slug=SLUG, version=1,
        visibility="link",
        request_json={"run_id": RUN, "gate": "concept_change", "narrative": "Story.",
                      "narration": NARRATION},
        **kw,
    )


def _cut(owner, review, cut_id, scene_ids, *, at, **kw):
    w = make_walkthrough(
        owner, kind="video", narrative_slug=SLUG, narrative_review_id=review.id,
        cut_id=cut_id, cut_scene_ids=scene_ids, title=kw.pop("title", f"Cut {cut_id}"),
        role=kw.pop("role", "clip"), **kw,
    )
    # Pin the clock so "latest wins" is not decided by sub-millisecond creation order.
    Walkthrough.objects.filter(pk=w.pk).update(created_at=at)
    w.refresh_from_db()
    return w


T0 = timezone.make_aware(dt.datetime(2026, 10, 7, 12, 0))


def _t(minutes):
    return T0 + dt.timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# The narrative page: a slot per cut, plus the hero
# ---------------------------------------------------------------------------


def test_each_cut_gets_its_own_slot_in_narration_order():
    u = make_user()
    v = _version(u)
    # Uploaded out of order: cut 3 first, then cut 1, then cut 2.
    c3 = _cut(u, v, "deliver", ["c3-a"], at=_t(1), duration_sec=31)
    c1 = _cut(u, v, "register", ["c1-a", "c1-b"], at=_t(2))
    c2 = _cut(u, v, "decide", ["c2-a"], at=_t(3))

    narrative = aggregate.build_narrative(SLUG)
    cuts = narrative["current_version"]["cuts"]
    assert [c["cut_id"] for c in cuts] == ["register", "decide", "deliver"]
    assert cuts[0]["scene_ids"] == ["c1-a", "c1-b"]
    assert cuts[0]["video_url"] == f"/w/{c1.workspace_id}/walkthrough/{c1.id}/content"
    assert narrative["versions"][0]["cuts"] == cuts
    # No explainer video → the hero is the FIRST cut, not the latest upload.
    assert narrative["current_version"]["video_url"] == f"/w/{c1.workspace_id}/walkthrough/{c1.id}/content"
    assert {c3.id, c2.id} == {c["walkthrough_id"] for c in cuts[1:]}
    assert cuts[2]["duration_sec"] == 31


def test_reuploading_a_cut_replaces_only_that_cut():
    u = make_user()
    v = _version(u)
    _cut(u, v, "register", ["c1-a"], at=_t(1))
    _cut(u, v, "decide", ["c2-a"], at=_t(2))
    newer = _cut(u, v, "register", ["c1-a"], at=_t(3), title="Register (re-cut)")

    cuts = aggregate.build_narrative(SLUG)["current_version"]["cuts"]
    assert [c["cut_id"] for c in cuts] == ["register", "decide"]
    assert cuts[0]["walkthrough_id"] == newer.id
    assert cuts[0]["title"] == "Register (re-cut)"


def test_a_cut_uploaded_as_hero_is_the_hero():
    u = make_user()
    v = _version(u)
    _cut(u, v, "register", ["c1-a"], at=_t(1))
    chosen = _cut(u, v, "decide", ["c2-a"], at=_t(2), role="hero_video")

    cv = aggregate.build_narrative(SLUG)["current_version"]
    assert cv["video_url"] == f"/w/{chosen.workspace_id}/walkthrough/{chosen.id}/content"


def test_an_explainer_video_stays_the_hero_and_is_not_a_cut():
    u = make_user()
    v = _version(u)
    hero = make_walkthrough(u, kind="video", narrative_slug=SLUG, narrative_review_id=v.id,
                            role="hero_video")
    Walkthrough.objects.filter(pk=hero.pk).update(created_at=_t(0))
    _cut(u, v, "register", ["c1-a"], at=_t(5))

    cv = aggregate.build_narrative(SLUG)["current_version"]
    # A later cut upload must not displace the narrative's own video.
    assert cv["video_url"] == f"/w/{hero.workspace_id}/walkthrough/{hero.id}/content"
    assert [c["cut_id"] for c in cv["cuts"]] == ["register"]


def test_an_explainer_narrative_has_no_cuts():
    u = make_user()
    v = _version(u)
    make_walkthrough(u, kind="video", narrative_slug=SLUG, narrative_review_id=v.id)
    cv = aggregate.build_narrative(SLUG)["current_version"]
    assert cv["cuts"] == []
    assert cv["video_url"]


# ---------------------------------------------------------------------------
# The review link: every cut beside its words, for guests too
# ---------------------------------------------------------------------------


def test_guest_on_the_review_link_gets_public_cuts_with_their_token():
    u = make_user()
    v = _version(u)
    pub = _cut(u, v, "register", ["c1-a", "c1-b"], at=_t(1), visibility="link",
               share_token="tok-register")
    priv = _cut(u, v, "decide", ["c2-a"], at=_t(2))

    resp = Client().get(f"/api/reviews/{v.id}/")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    cuts = body["cut_videos"]
    assert [c["cut_id"] for c in cuts] == ["register", "decide"]
    assert cuts[0]["scene_ids"] == ["c1-a", "c1-b"]
    assert cuts[0]["video_url"] == f"/w/{pub.workspace_id}/walkthrough/{pub.id}/content?t=tok-register"
    assert cuts[0]["video_viewer_url"] == f"/w/{pub.workspace_id}/walkthrough/{pub.id}?t=tok-register"
    # A private cut: the guest learns it exists, never gets a way to play it.
    assert cuts[1]["walkthrough_id"] == str(priv.id)
    assert cuts[1]["video_url"] is None
    assert cuts[1]["video_viewer_url"] is None
    # The hero (the first cut) rides along for the page's fallback.
    assert body["version_video"]["walkthrough_id"] == str(pub.id)

    # …and the public URL really plays for that guest.
    stream = Client().get(cuts[0]["video_url"])
    assert stream.status_code != 404


def test_member_on_the_review_gets_every_cut_tokenless():
    u = make_user()
    v = _version(u)
    _cut(u, v, "register", ["c1-a"], at=_t(1), visibility="link", share_token="tok-r")
    priv = _cut(u, v, "decide", ["c2-a"], at=_t(2))

    c = Client()
    c.force_login(u)
    cuts = c.get(f"/api/reviews/{v.id}/").json()["cut_videos"]
    assert cuts[1]["video_url"] == f"/w/{priv.workspace_id}/walkthrough/{priv.id}/content"
    assert all(x["video_url"] for x in cuts)


def test_a_video_from_another_workspace_never_reaches_the_review():
    """The version stamp is not proof — an upload may name any review id. Only
    videos in the review's own workspace are its videos."""
    from apps.runs.tests.factories import make_workspace

    u = make_user()
    v = _version(u)
    stranger = make_user("stranger@dimagi.com")
    _cut(stranger, v, "register", ["c1-a"], at=_t(1), visibility="link", share_token="x",
         workspace=make_workspace("elsewhere"))

    body = Client().get(f"/api/reviews/{v.id}/").json()
    assert body["cut_videos"] == []
    assert body["version_video"] is None


def test_a_review_with_no_pinned_videos_says_so():
    u = make_user()
    v = _version(u)
    body = Client().get(f"/api/reviews/{v.id}/").json()
    assert body["cut_videos"] == []
    assert body["version_video"] is None


# ---------------------------------------------------------------------------
# The upload names its cut
# ---------------------------------------------------------------------------


@pytest.fixture
def uploader(monkeypatch):
    monkeypatch.setattr(storage, "get_drive_client", lambda: FakeDriveClient())
    from apps.workspaces.models import WorkspaceMembership
    from apps.workspaces.services import ensure_member
    from apps.workspaces.testing import a_workspace

    user = make_user("uploader@dimagi.com")
    ensure_member(a_workspace(), user, WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(user)
    return c


def _upload(client, **fields):
    data = {
        "file": SimpleUploadedFile("cut.mp4", b"\x00\x00", content_type="video/mp4"),
        "kind": "video", "title": "Cut 1 · Register", "narrative_slug": SLUG,
        **fields,
    }
    return client.post("/api/walkthroughs/", data=data, format="multipart")


@override_settings(**_DDD_SETTINGS)
def test_upload_records_the_cut_and_its_scenes(uploader):
    rid = str(uuid.uuid4())
    resp = _upload(uploader, narrative_review_id=rid, cut_id="register",
                   cut_scene_ids="c1-a, c1-b")
    assert resp.status_code == 201, resp.content
    assert resp.json()["cut_id"] == "register"
    w = Walkthrough.objects.get(pk=resp.json()["id"])
    assert w.cut_id == "register"
    assert w.cut_scene_ids == ["c1-a", "c1-b"]
    assert str(w.narrative_review_id) == rid


@override_settings(**_DDD_SETTINGS)
@pytest.mark.parametrize(
    "fields",
    [
        {"cut_id": "register"},  # no version to pin it to
        {"cut_id": "has space", "narrative_review_id": "00000000-0000-0000-0000-000000000001"},
        {"cut_scene_ids": "c1-a", "narrative_review_id": "00000000-0000-0000-0000-000000000001"},
    ],
)
def test_upload_refuses_a_cut_that_could_not_be_placed(uploader, fields):
    resp = _upload(uploader, **fields)
    assert resp.status_code == 422, resp.content
    assert not Walkthrough.objects.exists()


@override_settings(**_DDD_SETTINGS)
def test_a_deck_cannot_be_a_cut(uploader):
    resp = uploader.post(
        "/api/walkthroughs/",
        data={"file": SimpleUploadedFile("d.html", b"<p>", content_type="text/html"),
              "kind": "html", "cut_id": "register",
              "narrative_review_id": str(uuid.uuid4())},
        format="multipart",
    )
    assert resp.status_code == 422, resp.content


@override_settings(**_DDD_SETTINGS)
@pytest.mark.parametrize("sent,stored", [("23.6", 24), ("31", 31), ("", None), ("abc", None)])
def test_upload_records_the_measured_duration(uploader, sent, stored):
    """A cut's length is the first thing a reviewer timing a demo needs, and the
    review page read null for every cut because nothing could send it."""
    resp = _upload(uploader, narrative_review_id=str(uuid.uuid4()), cut_id="register",
                   duration_sec=sent)
    assert resp.status_code == 201, resp.content
    assert Walkthrough.objects.get(pk=resp.json()["id"]).duration_sec == stored


def test_the_review_link_carries_the_cut_duration():
    u = make_user()
    v = _version(u)
    _cut(u, v, "register", ["c1-a"], at=_t(1), visibility="link", share_token="t", duration_sec=24)
    assert Client().get(f"/api/reviews/{v.id}/").json()["cut_videos"][0]["duration_sec"] == 24


# ---------------------------------------------------------------------------
# The demo brief: optional framing a publisher sends in request_json, so the
# review link reads without a companion doc. The server stores it verbatim; the
# page renders whatever is present.
# ---------------------------------------------------------------------------

BRIEF = {
    "title": "Part 1 — RUTF stock at scale",
    "audience": "Edesia / OES",
    "purpose": "One set of screens answers where every sachet is.",
    "data_disclosure": "All on-screen data is synthetic. Source: Labs programme 10112.",
    "running_order": ["register", "decide"],
    "short_version": {"label": "3-minute version", "cut_ids": ["register"]},
    "cuts": [{"id": "register", "screen_notes": "Overview page, cursor on the tender row.",
              "audience_note": "For Edesia: their order is the one on the road.",
              "duration_sec": 21}],
    "assumptions": ["Programme 10112 stands in for the real programme."],
    "divergences": [{"script": "four suppliers", "data": "three suppliers", "resolution": "re-record"}],
    "open_questions": ["Does OES want the forecast cut?"],
    "series": {"title": "Connect Supply demos", "url": "https://canopy.dimagi.com/w/connect/storyboard/x?t=y",
               "part": 1, "total": 3},
}


def test_the_demo_brief_round_trips_and_names_the_review():
    u = make_user()
    v = make_review(
        u, run_id=RUN, gate="concept_change", narrative_slug=SLUG, version=1, visibility="link",
        request_json={"run_id": RUN, "gate": "concept_change", "narrative": "Story.",
                      "narration": NARRATION, **BRIEF},
    )
    body = Client().get(f"/api/reviews/{v.id}/").json()
    assert body["title"] == "Part 1 — RUTF stock at scale"  # not "Chlorine"
    for key, value in BRIEF.items():
        assert body["request_json"][key] == value
