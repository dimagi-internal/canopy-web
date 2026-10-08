"""Every link the API hands out is under its workspace (canopy-web#1337).

Owner decision (Jonathan, 2026-10-08): one URL per artifact, `/w/<workspace>/…`,
and no flat form forwarded to it. A flat link went out in an external email
because the API that minted it still printed one, so this walks the JSON of
every artifact-bearing response — as a member, as a guest holding a token, and
from the create calls a tool prints — and fails on any URL field that is not
under `/w/<workspace>/`.

A URL field is any key named `url`, `*_url` or `href`. A field that is not an
artifact address goes in `NOT_ARTIFACT_LINKS`, with its reason.
"""
from __future__ import annotations

import json
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from apps.runs.tests.factories import make_review, make_user, make_walkthrough, make_workspace
from apps.harness.models import Turn
from apps.session_sharing.models import Session, ShareToken
from apps.walkthroughs import storage
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member

pytestmark = pytest.mark.django_db

PUBLIC = "https://canopy.example.org"
WS = "connect"
SLUG = "chlorine"
RUN = "chlorine-2026-10-07-002"

# Keys that hold a URL but not an artifact address. Each says why.
NOT_ARTIFACT_LINKS = {
    # A walkthrough's `links` are what its uploader attached (a PR, a doc):
    # someone else's addresses, stored verbatim.
    ("links", "url"),
}


@pytest.fixture(autouse=True)
def _env(settings, monkeypatch):
    settings.CANOPY_PUBLIC_BASE_URL = PUBLIC
    settings.WALKTHROUGHS_ENABLED = True
    monkeypatch.setattr(
        storage, "store_upload", lambda **kw: storage.StoredFile(file_id="f", folder_id="d")
    )


def _url_fields(node, path=()):
    """Yield (key path, value) for every URL-bearing field in a JSON body."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and (k in ("url", "href") or k.endswith("_url")):
                yield (*path, k), v
            else:
                yield from _url_fields(v, (*path, k))
    elif isinstance(node, list):
        for item in node:
            yield from _url_fields(item, path)


def _unscoped(body) -> list[str]:
    bad = []
    for keys, value in _url_fields(body):
        if tuple(keys[-2:]) in NOT_ARTIFACT_LINKS:
            continue
        rel = value.removeprefix(PUBLIC)
        if not rel.startswith(f"/w/{WS}/"):
            bad.append(f"{'.'.join(keys)} = {value}")
    return bad


@pytest.fixture
def seeded():
    """One of each artifact in `connect`: a narrative version with a public cut,
    a run with a hero video, a shared session."""
    ws = make_workspace(WS)
    owner = make_user("owner@dimagi.com")
    review = make_review(
        owner, workspace=ws, run_id=RUN, gate="concept_change", narrative_slug=SLUG,
        version=1, visibility="link",
        request_json={"run_id": RUN, "gate": "concept_change", "narrative": "Story.",
                      "narration": [{"id": "s1", "scene": 1, "title": "One", "text": "One."}]},
    )
    review.ensure_share_token()
    make_walkthrough(
        owner, workspace=ws, kind="video", narrative_slug=SLUG, narrative_review_id=review.id,
        cut_id="one", cut_scene_ids=["s1"], role="clip", visibility="link", share_token="tok-cut",
    )
    make_walkthrough(
        owner, workspace=ws, kind="video", run_id=RUN, narrative_slug=SLUG, role="hero",
        visibility="link", share_token="tok-run",
    )
    session = Session.objects.create(owner=owner, workspace=ws, title="t", visibility="link")
    share = ShareToken.objects.create(session=session, created_by=owner).token
    # An agent turn whose transcript is that shared session.
    from types import SimpleNamespace

    from apps.agents import services as agents

    agent = agents.upsert_agent(
        SimpleNamespace(slug="echo", name="Echo", description="", persona="", email="", avatar_url=""),
        workspace=ws,
    )
    Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_API, idempotency_key="t1", status="done",
        session_slug=session.slug, share_token=share,
    )
    # A storyboard (the shared arc) with its share link minted.
    from apps.storyboards.models import Act, Entry, Storyboard

    board = Storyboard.objects.create(slug="arc", title="Arc", workspace=ws)
    act = Act.objects.create(storyboard=board, title="One", position=0)
    Entry.objects.create(act=act, narrative_slug=SLUG, position=0)
    board.ensure_share_token()
    # An owner reads every turn's content (apps/harness/turn_access.py).
    M.objects.filter(workspace=ws, user=owner).update(role=M.OWNER)
    return {"owner": owner, "review": review, "share": share, "board": board}


def _member(owner) -> Client:
    c = Client()
    c.force_login(owner)
    return c


def _reads(seeded):
    """(label, client, path) for every artifact-bearing read."""
    member, guest = _member(seeded["owner"]), Client()
    review = seeded["review"]
    return [
        ("review, member", member, f"/api/reviews/{review.id}/"),
        ("review, guest", guest, f"/api/reviews/{review.id}/?t={review.share_token}"),
        ("narrative", member, f"/api/w/{WS}/ddd/narratives/{SLUG}/"),
        ("narratives", member, f"/api/w/{WS}/ddd/narratives/"),
        ("run", member, f"/api/w/{WS}/ddd/runs/{RUN}/"),
        ("walkthroughs", member, f"/api/w/{WS}/walkthroughs/"),
        ("shared session, guest", guest, f"/api/share/{seeded['share']}"),
        ("shared sessions", member, f"/api/w/{WS}/sessions/"),
        ("timeline", member, f"/api/w/{WS}/timeline/"),
        ("agent turns", member, "/api/agents/echo/turns/"),
        ("storyboards", member, f"/api/w/{WS}/storyboards/"),
        ("storyboard, guest", guest, f"/api/storyboards/arc?t={seeded['board'].share_token}&ws={WS}"),
        ("release, member", member, f"/api/ddd/release/{RUN}/"),
        ("release, guest", guest, f"/api/ddd/release/{RUN}/?t=tok-run&ws={WS}"),
    ]


def test_every_read_hands_out_only_scoped_links(seeded):
    seen = 0
    for label, client, path in _reads(seeded):
        resp = client.get(path)
        assert resp.status_code == 200, (label, resp.content[:300])
        body = resp.json()
        seen += sum(1 for _ in _url_fields(body))
        assert _unscoped(body) == [], label
    # The walk found the links it exists to check — a schema rename that left
    # it looking at nothing would otherwise pass silently.
    assert seen >= 20, seen


def test_walkthrough_detail_hands_out_only_scoped_links(seeded):
    from apps.walkthroughs.models import Walkthrough

    member = _member(seeded["owner"])
    for w in Walkthrough.objects.all():
        body = member.get(f"/api/walkthroughs/{w.id}/").json()
        assert body["share_url"], body
        assert _unscoped(body) == []


def test_every_create_prints_a_scoped_link():
    """What `scripts.ddd.narrative post` and walkthrough-share print comes from
    these responses — the flat link in the chlorine email started here."""
    ws = make_workspace(WS)
    owner = a_member(ws, email="ace@dimagi-ai.com", role=M.EDITOR)
    c = _member(owner)
    review = c.post(
        f"/api/w/{WS}/reviews/",
        data=json.dumps({"visibility": "link",
                         "request_json": {"run_id": RUN, "gate": "concept_change",
                                          "narrative_slug": SLUG}}),
        content_type="application/json",
    )
    walkthrough = c.post(
        f"/api/w/{WS}/walkthroughs/",
        data={"file": SimpleUploadedFile("v.mp4", b"x", content_type="video/mp4"),
              "kind": "video", "visibility": "link"},
        format="multipart",
        HTTP_HOST="localhost",
    )
    rows = [{"type": "system", "subtype": "init", "session_id": f"s-{uuid.uuid4()}"},
            {"type": "user", "message": {"content": "hi"}}]
    session = c.post(
        f"/api/w/{WS}/sessions/upload",
        data={"file": SimpleUploadedFile("s.jsonl", ("\n".join(map(json.dumps, rows)) + "\n").encode()),
              "visibility": "link"},
        format="multipart",
    )
    for resp in (review, walkthrough, session):
        assert resp.status_code == 201, resp.content
        body = resp.json()
        assert any(True for _ in _url_fields(body)), body
        assert _unscoped(body) == []
        # Never the request's host: an in-process MCP call has none, and that
        # minted `https://localhost/…` (canopy-web#1289).
        assert "localhost" not in json.dumps(body)


@override_settings(REQUIRE_AUTH=True)
def test_a_scoped_link_the_api_hands_out_actually_opens(seeded):
    """The guest's cut URL streams at the address it was given — no redirect."""
    from unittest.mock import patch

    review = seeded["review"]
    body = Client().get(f"/api/reviews/{review.id}/?t={review.share_token}").json()
    url = body["cut_videos"][0]["video_url"]
    with patch("apps.walkthroughs.streaming.storage.download", return_value=(b"data", 0, 3, 4)):
        resp = Client().get(url)
    assert resp.status_code == 200


def test_a_turns_transcript_link_is_under_the_workspace_it_was_shared_from(seeded):
    """The Turns card used to build `/share/<token>` itself."""
    item = _member(seeded["owner"]).get("/api/agents/echo/turns/").json()["items"][0]
    assert item["share_url"] == f"{PUBLIC}/w/{WS}/share/{seeded['share']}"


def test_the_shared_arc_and_release_links_are_scoped(seeded):
    """The storyboard's share link (list, mint, re-mint) and the release page's
    own address are `/w/<ws>/…` — the flat /storyboard/ and /ddd-release/ are a
    plain 404 (canopy-web#1337)."""
    member = _member(seeded["owner"])
    for resp in (member.post("/api/storyboards/arc/share"),
                 member.post("/api/storyboards/arc/rotate-token")):
        assert resp.status_code == 200, resp.content
        url = resp.json()["share_url"]
        assert url.startswith(f"{PUBLIC}/w/{WS}/storyboard/arc?t="), url
    release = Client().get(f"/api/ddd/release/{RUN}/?t=tok-run&ws={WS}").json()
    assert release["share_url"] == f"{PUBLIC}/w/{WS}/ddd-release/{SLUG}/{RUN}?t=tok-run"
