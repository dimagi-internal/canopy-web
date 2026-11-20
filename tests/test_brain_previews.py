"""Preview snapshots as a data source (`apps/brain/snapshots.py`, `view.py`, `api_previews.py`).

Making one writes that row and nothing of the brain; every brain screen's route answers the same shape for `live` and
`preview:<id>`; a preview is its owner's alone, as a person; recommendations cite only the owner's readable sessions of
the snapshot and are scrubbed; a removal reaches every snapshot; and nothing that decides anything reads a snapshot."""
from __future__ import annotations

import pathlib
import re

import pytest
from django.db import connection
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.agents.models import Agent, AgentTask
from apps.api.errors import ProblemError
from apps.brain import cascade, consent, snapshots, view
from apps.brain.models import BrainPreviewSnapshot, BrainSource, SourceLink
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.harness import act_as, turn_agent_tokens
from apps.harness.models import Runner, Turn
from apps.mcp import api_tools
from tests.fixtures.brain_project_leg import SECRET, client, project_leg_world
from tests.fixtures.brain_world import member, user
from tests.token_helpers import bearer_headers, pat_token

pytestmark = pytest.mark.django_db

PREVIEWS = "/api/brain/previews/"


@pytest.fixture()
def w(settings):
    settings.BRAIN_READS_ENABLED = True
    out = project_leg_world()
    # A second session of alice's that the dispatched layer WOULD tie to the project: stored nowhere yet.
    fresh = Session.objects.create(workspace=out["ws"], created_by=out["alice"], origin=Session.ORIGIN_WEB,
                                   title="tender work")
    task = AgentTask.objects.create(agent=out["lead"], project=out["project"], ext_id="T1", title="tender")
    Turn.objects.create(chat_session=fresh, raised_from_task=task, prompt="go", idempotency_key="k1")
    out["fresh"], out["task"] = fresh, task
    return out


def _make(w, who="alice", c=None) -> dict:
    r = (c or client(w[who])).post(PREVIEWS, {}, content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()


def _writes(ctx) -> list[str]:
    return [q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().split(" ", 1)[0].upper() in ("INSERT", "UPDATE", "DELETE")]


def _routes(w, pid=None):
    """Every route that takes `source`, as (url, params)."""
    src = {"source": f"preview:{pid}"} if pid else {}
    return [("/api/brain/state", src), ("/api/brain/state/links", src), ("/api/brain/state/runs", src),
            ("/api/brain/would-read", src), ("/api/brain/outputs/", src),
            (f"/api/me/learning/sessions/{w['fresh'].pk}/", src),
            (f"/api/brain/workspaces/acme/projects/{w['project'].handle}/sessions/", src)]


def test_making_a_preview_writes_its_row_and_nothing_of_the_brain(w, django_capture_on_commit_callbacks):
    links, sources_before = SourceLink.objects.count(), BrainSource.objects.count()
    alice = client(w["alice"])
    with django_capture_on_commit_callbacks() as callbacks, CaptureQueriesContext(connection) as ctx:
        body = _make(w, c=alice)
    tables = {q.split(" ", 3)[2 if q.upper().startswith(("INSERT", "DELETE")) else 1].strip('"') for q in _writes(ctx)}
    assert tables <= {"brain_brainpreviewsnapshot", "django_session"}, _writes(ctx)
    assert callbacks == []
    assert SourceLink.objects.count() == links and BrainSource.objects.count() == sources_before
    assert body["source"] == f"preview:{body['id']}" and body["sessions"] == 2 and body["outputs"] == 0


def test_the_preview_shows_what_the_live_code_would_do_on_the_same_screens(w):
    pid = _make(w)["id"]
    alice = client(w["alice"])
    live = alice.get(f"/api/me/learning/sessions/{w['fresh'].pk}/").json()
    assert live["projects"] == [] and live["source"]["kind"] == "live"
    seen = alice.get(f"/api/me/learning/sessions/{w['fresh'].pk}/", {"source": f"preview:{pid}"}).json()
    assert seen["projects"] == [{"project": "supply", "name": "Supply", "tier": "core", "how": "dispatched"}]
    assert seen["source"]["kind"] == "preview" and seen["source"]["id"] == pid
    listed = alice.get("/api/brain/workspaces/acme/projects/supply/sessions/", {"source": f"preview:{pid}"}).json()
    assert {r["session_id"] for r in listed} == {str(w["session"].pk), str(w["fresh"].pk)}
    groups = alice.get("/api/brain/state", {"source": f"preview:{pid}"}).json()["links"]
    assert groups == [{"project": "supply", "name": "Supply", "workspace": "acme", "tier": "core", "count": 2}]
    rows = alice.get("/api/brain/would-read", {"source": f"preview:{pid}"}).json()
    assert rows["total"] == 2 and rows["counts"]["links"] == 1
    # The link read a graph takes.
    got = view.links(w["alice"], [w["fresh"].pk], source=view.Source(view.PREVIEW, BrainPreviewSnapshot.objects.get()))
    assert [(x["subject"], x["tier"], x["how"]) for x in got] == [(str(w["project"].pk), "core", "dispatched")]
    assert view.links(w["alice"], [w["fresh"].pk]) == []
    assert SourceLink.objects.filter(source__session=w["fresh"]).count() == 0       # still stored nowhere


def test_live_and_preview_answer_the_same_shape(w):
    pid = _make(w)["id"]
    snapshot = BrainPreviewSnapshot.objects.get()
    client(w["alice"]).put(f"{PREVIEWS}{pid}/outputs/", {"outputs": [
        {"kind": "finding", "title": "t", "body": {"a": 1}, "sessions": [str(w["fresh"].pk)]}]},
        content_type="application/json")
    del snapshot
    alice = client(w["alice"])

    def keys(body):
        if isinstance(body, list):
            return ("list", keys(body[0]) if body else None)
        return tuple(sorted(body))

    from apps.brain.models import BrainOutput
    BrainOutput.objects.create(capability=w["sys"], kind="note", workspace=w["ws"], subject_type="system",
                               subject="", title="live one")
    w["alice"].is_superuser = True
    w["alice"].save(update_fields=["is_superuser"])
    for (url, live_params), (_u, prev_params) in zip(_routes(w), _routes(w, pid), strict=True):
        a, b = alice.get(url, live_params), alice.get(url, prev_params)
        assert a.status_code == b.status_code == 200, (url, a.content, b.content)
        assert keys(a.json()) == keys(b.json()), url


def test_a_preview_is_its_owner_s_alone_as_a_person(w):
    pid = _make(w)["id"]
    login = user("hal_login")
    member(login, w["ws"])
    hal = Agent.objects.create(slug="hal", name="Hal", owner=w["owner"], workspace=w["ws"], user=login)
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"],
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    turn = Turn.objects.create(agent=hal, prompt="x", idempotency_key="k2", status=Turn.RUNNING, claimed_by=box,
                               initiator_kind="user", initiator_user=w["alice"])
    turn_login = turn_agent_tokens.issue(turn, box)["token"]
    for url, params in _routes(w, pid):
        assert client(w["bob"]).get(url, params).status_code == 404, url                     # someone else's
        assert Client().get(url, params, **bearer_headers(pat_token(login))).status_code in (403, 404), url
        assert Client().get(url, params, **bearer_headers(turn_login)).status_code in (403, 404), url
        assert client(w["alice"]).get(url, params, HTTP_X_CANOPY_TURN_ID="x").status_code == 403, url
        assert client(w["alice"]).get(url, {"source": "nonsense"}).status_code == 422, url
    for url in (PREVIEWS, f"{PREVIEWS}{pid}/outputs/"):
        assert act_as.route_refusal("POST" if url == PREVIEWS else "PUT", url, "read", is_tool=False) is not None
    assert Client().post(PREVIEWS, {}, content_type="application/json",
                         **bearer_headers(pat_token(login))).status_code == 403
    assert client(w["bob"]).delete(f"{PREVIEWS}{pid}/").status_code == 404
    for name in ("create_brain_preview", "list_brain_previews", "delete_brain_preview", "put_brain_preview_outputs"):
        assert name in api_tools.EXCLUDED
    # An MCP call naming a preview is refused before the snapshot is looked up, even by its owner.
    request = RequestFactory().get("/api/brain/outputs/")
    request.user, request.via_mcp = w["alice"], True
    with pytest.raises(ProblemError) as refused:
        view.resolve(request, f"preview:{pid}")
    assert refused.value.status_code == 403


def test_a_leg_7_only_reader_s_preview_holds_none_of_it(w):
    body = _make(w, "reader")
    assert body["sessions"] == 0
    pid = body["id"]
    assert client(w["reader"]).get("/api/brain/would-read", {"source": f"preview:{pid}"}).json()["sessions"] == []


def test_a_session_the_owner_stops_reading_leaves_every_read(w):
    pid = _make(w, "bob")["id"]
    bob = client(w["bob"])
    assert bob.get("/api/brain/would-read", {"source": f"preview:{pid}"}).json()["total"] == 1
    SessionParticipant.objects.filter(session=w["session"], user=w["bob"]).delete()
    assert bob.get("/api/brain/would-read", {"source": f"preview:{pid}"}).json()["total"] == 0
    assert bob.get("/api/brain/state", {"source": f"preview:{pid}"}).json()["links"] == []


def test_recommendations_cite_the_snapshot_and_are_scrubbed(w):
    pid = _make(w)["id"]
    alice = client(w["alice"])
    ok = alice.put(f"{PREVIEWS}{pid}/outputs/", {"outputs": [
        {"kind": "finding", "title": f"leaked {SECRET}", "body": {SECRET: f"x {SECRET}"},
         "sessions": [str(w["fresh"].pk)]}]}, content_type="application/json")
    assert ok.status_code == 200 and ok.json()["outputs"] == 1
    shown = alice.get("/api/brain/outputs/", {"source": f"preview:{pid}"}).json()
    assert len(shown) == 1 and SECRET not in str(shown) and shown[0]["subject_type"] == "system"
    assert shown[0]["sessions"] == [str(w["fresh"].pk)] and shown[0]["source"]["id"] == pid
    assert alice.get("/api/brain/outputs/").json() == []                        # live: nothing was made
    other = Session.objects.create(workspace=w["ws"], created_by=w["carol"], origin=Session.ORIGIN_WEB)
    bad = alice.put(f"{PREVIEWS}{pid}/outputs/", {"outputs": [
        {"kind": "finding", "body": {}, "sessions": [str(other.pk)]}]}, content_type="application/json")
    assert bad.status_code == 422
    assert client(w["bob"]).put(f"{PREVIEWS}{pid}/outputs/", {"outputs": []},
                                content_type="application/json").status_code == 404


def test_a_removal_drops_the_session_and_what_cites_it_from_every_snapshot(w):
    pid = _make(w)["id"]
    client(w["alice"]).put(f"{PREVIEWS}{pid}/outputs/", {"outputs": [
        {"kind": "finding", "body": {}, "sessions": [str(w["fresh"].pk)]},
        {"kind": "finding", "body": {}, "sessions": [str(w["session"].pk)]}]}, content_type="application/json")
    consent.exclude(w["bob"], w["source"])                     # bob takes the shared session out
    cascade.sweep([w["source"].pk])
    snap = BrainPreviewSnapshot.objects.get()
    assert str(w["session"].pk) not in snap.session_ids
    assert [o["sessions"] for o in snap.outputs] == [[str(w["fresh"].pk)]]
    assert all(x["session_id"] != str(w["session"].pk) for x in snap.payload["links"])
    # Its words deleted: gone at the next full sweep, and from every read at once.
    Session.objects.filter(pk=w["fresh"].pk).update(content_deleted_at=timezone.now())
    assert client(w["alice"]).get("/api/brain/would-read", {"source": f"preview:{pid}"}).json()["total"] == 0
    cascade.sweep()
    snap.refresh_from_db()
    assert snap.session_ids == [] and snap.outputs == []


def test_a_preview_expires(w):
    pid = _make(w)["id"]
    BrainPreviewSnapshot.objects.filter(pk=pid).update(expires_at=timezone.now())
    assert client(w["alice"]).get("/api/brain/state", {"source": f"preview:{pid}"}).status_code == 404
    assert client(w["alice"]).get(PREVIEWS).json() == []
    assert snapshots.expire() == 1 and not BrainPreviewSnapshot.objects.exists()


def test_nothing_that_decides_reads_a_snapshot():
    """The fence: a learner's read, `eligible`, association and the cascade's judgement never name a snapshot. The
    cascade only calls the store registered with it (`register_store(..., by_session=True)`)."""
    root = pathlib.Path(__file__).resolve().parents[1] / "apps" / "brain"
    deciders = ["eligibility.py", "reading.py", "emit.py", "readers.py", "associate.py", "cascade.py", "runs.py",
                "schedule.py", "consent.py", "disclosure.py", "outputs.py", "registry.py", "sources.py",
                "participants.py", "hcp_store.py", "api_run.py"]
    pattern = re.compile(r"BrainPreviewSnapshot|\bsnapshots\b|\bview\b import|from \. import .*\bview\b")
    offenders = [name for name in deciders if pattern.search((root / name).read_text())]
    assert offenders == []
