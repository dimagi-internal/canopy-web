"""Huddles — a team of agents syncs, led by one of them (canopy
docs/superpowers/specs/2026-10-06-huddle-design.md). canopy-web stores NOTHING new
for a huddle: it is derived from tagged turns, close-out reports and board tasks.
These tests pin the tags going in (close-out `origin_ref`, turn filters) and the
derived view coming out (`/api/huddles/`)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@example.org", "pw")


@pytest.fixture()
def ws(owner):
    w = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=w, role=WorkspaceMembership.OWNER)
    return w


@pytest.fixture()
def agents(ws):
    return {s: Agent.objects.create(slug=s, name=s.title(), workspace=ws)
            for s in ("ada", "eva", "echo")}


def _client(user):
    c = Client()
    c.force_login(user)
    return c


# ── A1: close-out origin_ref + turn filters ──────────────────────────────────


def test_closeout_records_origin_ref(owner, agents):
    r = _client(owner).post("/api/agents/ada/turns/", {
        "cli_session_id": "huddle:work-fleet-20261006", "title": "Huddle work-fleet-20261006",
        "origin_ref": {"kind": "huddle", "huddle": "work-fleet-20261006"}},
        content_type="application/json")
    assert r.status_code == 201, r.content
    t = Turn.objects.get(cli_session_id="huddle:work-fleet-20261006")
    assert t.origin_ref["kind"] == "huddle"


def test_closeout_merges_origin_ref_into_existing_row(owner, agents):
    body = {"cli_session_id": "huddle:h1", "title": "x", "origin_ref": {"kind": "huddle", "huddle": "h1"}}
    _client(owner).post("/api/agents/ada/turns/", body, content_type="application/json")
    body2 = {**body, "summary": "done", "origin_ref": {"finished_at": "2026-10-06T12:00:00Z"}}
    _client(owner).post("/api/agents/ada/turns/", body2, content_type="application/json")
    t = Turn.objects.get(cli_session_id="huddle:h1")
    assert t.origin_ref == {"kind": "huddle", "huddle": "h1", "finished_at": "2026-10-06T12:00:00Z"}
    assert t.report_summary == "done"


def test_closeout_without_origin_ref_keeps_existing(owner, agents):
    body = {"cli_session_id": "huddle:h1", "title": "x", "origin_ref": {"kind": "huddle", "huddle": "h1"}}
    c = _client(owner)
    c.post("/api/agents/ada/turns/", body, content_type="application/json")
    c.post("/api/agents/ada/turns/", {"cli_session_id": "huddle:h1", "title": "y"},
           content_type="application/json")
    assert Turn.objects.get(cli_session_id="huddle:h1").origin_ref == {"kind": "huddle", "huddle": "h1"}


def test_list_turns_filters_by_huddle_and_parent(owner, agents):
    anchor = Turn.objects.create(agent=agents["ada"], idempotency_key="a",
                                 origin_ref={"kind": "huddle", "huddle": "h1"})
    mine = Turn.objects.create(agent=agents["eva"], idempotency_key="b", parent_turn=anchor,
                               origin_ref={"kind": "huddle_round", "huddle": "h1", "round": 1})
    Turn.objects.create(agent=agents["eva"], idempotency_key="c",
                        origin_ref={"kind": "huddle_round", "huddle": "h2", "round": 1})
    c = _client(owner)
    ids = {t["id"] for t in c.get("/api/harness/turns/?huddle=h1").json()}
    assert ids == {str(anchor.id), str(mine.id)}
    ids = {t["id"] for t in c.get(f"/api/harness/turns/?parent_turn={anchor.id}").json()}
    assert ids == {str(mine.id)}


def test_turn_filters_stay_tenant_filtered(agents):
    Turn.objects.create(agent=agents["ada"], idempotency_key="a",
                        origin_ref={"kind": "huddle", "huddle": "h1"})
    stranger = User.objects.create_user("s", "s@example.org", "pw")
    assert _client(stranger).get("/api/harness/turns/?huddle=h1").json() == []


# ── A2: the derived huddle API ───────────────────────────────────────────────

BLOCK = '```huddle\n{"huddle": "h1", "round": 1, "member": "eva", "worked_on": ["x"]}\n```'


def _huddle(agents, **anchor_ref):
    anchor = Turn.objects.create(
        agent=agents["ada"], idempotency_key="anchor", cli_session_id="huddle:h1",
        report_summary="the leader's digest",
        origin_ref={"kind": "huddle", "huddle": "h1", "type": "work", "team": "fleet",
                    "leader": "ada", "members": ["eva", "echo"], **anchor_ref})
    r1 = Turn.objects.create(
        agent=agents["eva"], idempotency_key="huddle-h1-eva-r1-a1", parent_turn=anchor,
        status=Turn.DONE, prompt="report please",
        origin_ref={"kind": "huddle_round", "huddle": "h1", "round": 1, "member": "eva", "attempt": 1})
    return anchor, r1


def test_extract_block_checks_huddle_and_round():
    from apps.huddles.services import extract_block
    b, err = extract_block("blah\n" + BLOCK, "h1", 1)
    assert b["worked_on"] == ["x"] and err == ""
    assert extract_block(BLOCK, "h2", 1)[0] is None
    assert extract_block(BLOCK, "h1", 2)[0] is None
    b, err = extract_block("```huddle\n{not json\n```", "h1", 1)
    assert b is None and "JSON" in err
    assert extract_block("", "h1", 1) == (None, "")


def test_extract_block_accepts_bare_json_summary():
    from apps.huddles.services import extract_block
    text = '{"huddle": "h1", "round": 2, "member": "ace", "worked_on": ["y"]}'
    b, err = extract_block(text, "h1", 2)
    assert b["member"] == "ace" and err == ""


def test_extract_block_accepts_json_fence():
    from apps.huddles.services import extract_block
    text = 'done.\n```json\n{"huddle": "h1", "round": 1, "member": "eva"}\n```\n'
    b, err = extract_block(text, "h1", 1)
    assert b["member"] == "eva" and err == ""
    b, _ = extract_block(text.replace("```json", "```"), "h1", 1)
    assert b["member"] == "eva"
    # A JSON fence without a "huddle" key is just prose, not a reply.
    assert extract_block('```json\n{"a": 1}\n```', "h1", 1) == (None, "")


def test_extract_block_bare_json_other_round_not_matched():
    from apps.huddles.services import extract_block
    text = '{"huddle": "h1", "round": 2, "member": "ace"}'
    b, err = extract_block(text, "h1", 1)
    assert b is None and "round 2" in err


def test_extract_block_fenced_huddle_wins_over_loose_json():
    from apps.huddles.services import extract_block
    loose = '\n```json\n{"huddle": "h1", "round": 1, "member": "eva", "worked_on": ["loose"]}\n```'
    b, _ = extract_block(BLOCK + loose, "h1", 1)
    assert b["worked_on"] == ["x"]


def test_detail_reads_reply_from_closeout_row(owner, agents):
    anchor, r1 = _huddle(agents)
    Turn.objects.create(agent=agents["eva"], idempotency_key="co", cli_session_id="s-eva",
                        report_summary=BLOCK, status=Turn.DONE)
    d = _client(owner).get("/api/huddles/h1").json()
    cell = next(c for c in d["cells"] if c["member"] == "eva" and c["round"] == 1)
    assert cell["block"]["worked_on"] == ["x"]
    assert cell["reply_source"] == "closeout"
    assert cell["prompt"] == "report please" and cell["content_hidden"] is False
    assert d["leader"] == "ada" and d["members"] == ["eva", "echo"]
    assert d["type"] == "work" and d["team"] == "fleet" and d["summary"] == "the leader's digest"
    assert d["deadline_at"] is not None and d["finished"] is False


def test_detail_reads_reply_from_round_turn_itself(owner, agents):
    # A cloud runner's close-out attaches to the dispatch row it closes.
    anchor, r1 = _huddle(agents)
    Turn.objects.filter(pk=r1.pk).update(report_summary="done.\n" + BLOCK)
    cell = _client(owner).get("/api/huddles/h1").json()["cells"][0]
    assert cell["reply_source"] == "closeout" and cell["block"]["member"] == "eva"


def test_wrong_huddle_block_is_ignored_with_reason(owner, agents):
    anchor, r1 = _huddle(agents)
    Turn.objects.filter(pk=r1.pk).update(report_summary=BLOCK.replace('"h1"', '"h0"'))
    cell = _client(owner).get("/api/huddles/h1").json()["cells"][0]
    assert cell["block"] is None and cell["reply_source"] == "none"
    assert "h0" in cell["reply_error"]


def test_detail_falls_back_to_transcript(owner, agents):
    import json as _json

    from apps.harness import ledger
    anchor, r1 = _huddle(agents)
    line = _json.dumps({"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "text", "text": "Here you go\n" + BLOCK}]}})
    ledger.append_transcript(r1, [line])
    cell = _client(owner).get("/api/huddles/h1").json()["cells"][0]
    assert cell["has_transcript"] is True
    assert cell["reply_source"] == "transcript" and cell["block"]["worked_on"] == ["x"]


def test_detail_keeps_latest_attempt_only(owner, agents):
    anchor, r1 = _huddle(agents)
    Turn.objects.create(agent=agents["eva"], idempotency_key="huddle-h1-eva-r1-a2", parent_turn=anchor,
                        origin_ref={"kind": "huddle_round", "huddle": "h1", "round": 1,
                                    "member": "eva", "attempt": 2})
    cells = [c for c in _client(owner).get("/api/huddles/h1").json()["cells"] if c["member"] == "eva"]
    assert [c["attempt"] for c in cells] == [2]


def test_finished_and_rounds_dispatched(owner, agents):
    anchor, r1 = _huddle(agents, finished_at="2026-10-06T12:00:00Z")
    Turn.objects.create(agent=agents["echo"], idempotency_key="huddle-h1-echo-r2-a1", parent_turn=anchor,
                        origin_ref={"kind": "huddle_round", "huddle": "h1", "round": 2,
                                    "member": "echo", "attempt": 1})
    row = _client(owner).get("/api/huddles/").json()[0]
    assert row["finished"] is True and row["rounds_dispatched"] == 2


def test_outputs_are_tasks_pointing_at_the_huddle(owner, agents):
    _huddle(agents)
    p = AgentProject.objects.create(agent=agents["eva"], ext_id="P3", name="Q4 pipeline")
    AgentTask.objects.create(agent=agents["eva"], ext_id="T41", title="Q4 brief", project=p,
                             status="suggested", assigned="eva",
                             next_action="BLOCKED on creds: provision the SF key on the runner",
                             source_url="https://x/w/connect/huddles/h1")
    AgentTask.objects.create(agent=agents["eva"], ext_id="T42", title="other",
                             source_url="https://x/w/connect/huddles/h10")
    d = _client(owner).get("/api/huddles/h1").json()
    out = d["outputs"]
    assert [o["ext_id"] for o in out] == ["T41"]
    assert out[0]["project"] == "P3" and out[0]["status"] == "suggested"
    assert out[0]["url"] == "/w/connect/agents/eva/work"
    assert out[0]["next_action"] == "BLOCKED on creds: provision the SF key on the runner"
    assert out[0]["updated_at"]
    assert _client(owner).get("/api/huddles/").json()[0]["outcome_count"] == 1


def test_list_and_404(owner, agents):
    _huddle(agents)
    c = _client(owner)
    rows = c.get("/api/huddles/").json()
    assert [r["id"] for r in rows] == ["h1"]
    assert rows[0]["anchor_turn_id"] and rows[0]["rounds_dispatched"] == 1
    assert c.get("/api/huddles/?agent=echo").json()[0]["id"] == "h1"
    assert c.get("/api/huddles/?agent=ada").json()[0]["id"] == "h1"
    assert c.get("/api/huddles/?agent=hal").json() == []
    assert c.get("/api/huddles/nope").status_code == 404


def test_workspace_scoped_route(owner, agents):
    _huddle(agents)
    assert [r["id"] for r in _client(owner).get("/api/w/connect/huddles/").json()] == ["h1"]


def test_stranger_sees_nothing(agents):
    _huddle(agents)
    stranger = User.objects.create_user("s", "s@example.org", "pw")
    assert _client(stranger).get("/api/huddles/").json() == []
    assert _client(stranger).get("/api/huddles/h1").status_code == 404


def test_hidden_content_viewer_gets_status_only(ws, agents):
    """An editor sees THAT the rounds ran, never what was asked or answered —
    the same line /api/harness/turns/{id}/messages draws (turn_access)."""
    anchor, r1 = _huddle(agents)
    Turn.objects.filter(pk=r1.pk).update(report_summary=BLOCK)
    editor = User.objects.create_user("ed", "ed@example.org", "pw")
    WorkspaceMembership.objects.create(user=editor, workspace=ws, role=WorkspaceMembership.EDITOR)
    c = _client(editor)
    assert c.get(f"/api/harness/turns/{r1.id}/messages").status_code == 404  # the line we mirror
    d = c.get("/api/huddles/h1").json()
    cell = d["cells"][0]
    assert cell["content_hidden"] is True and cell["status"] == "done"
    assert cell["prompt"] == "" and cell["block"] is None and cell["reply_source"] == "none"
    assert d["summary"] == ""
    assert "report please" not in str(d) and "worked_on" not in str(d)


# ── A huddle spans workspaces ────────────────────────────────────────────────


def _cross_ws_huddle(owner, agents, ws):
    """Anchor + eva in `connect` (ws A); ace's round turn, reply and task in `dimagi` (B)."""
    b = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=b, role=WorkspaceMembership.OWNER)
    ace = Agent.objects.create(slug="ace", name="Ace", workspace=b)
    anchor, _ = _huddle(agents)
    Turn.objects.create(
        agent=ace, idempotency_key="huddle-h1-ace-r1-a1", parent_turn=anchor,
        status=Turn.DONE, prompt="report please",
        report_summary='{"huddle": "h1", "round": 1, "member": "ace", "worked_on": ["z"]}',
        origin_ref={"kind": "huddle_round", "huddle": "h1", "round": 1, "member": "ace", "attempt": 1})
    AgentTask.objects.create(agent=ace, ext_id="T9", title="ace task",
                             source_url="https://x/w/connect/huddles/h1")
    return b


def test_scoped_detail_spans_callers_workspaces(owner, ws, agents):
    _cross_ws_huddle(owner, agents, ws)
    c = _client(owner)
    d = c.get("/api/w/connect/huddles/h1").json()
    cells = {x["member"]: x for x in d["cells"]}
    assert set(cells) == {"eva", "ace"}
    assert cells["ace"]["block"]["worked_on"] == ["z"]
    assert [(o["ext_id"], o["url"]) for o in d["outputs"]] == [("T9", "/w/dimagi/agents/ace/work")]
    assert c.get("/api/w/connect/huddles/").json()[0]["outcome_count"] == 1


def test_member_of_one_workspace_sees_no_other_workspace(owner, ws, agents):
    _cross_ws_huddle(owner, agents, ws)
    only_a = User.objects.create_user("a", "a@example.org", "pw")
    WorkspaceMembership.objects.create(user=only_a, workspace=ws, role=WorkspaceMembership.OWNER)
    c = _client(only_a)
    for path in ("/api/w/connect/huddles/h1", "/api/huddles/h1"):
        d = c.get(path).json()
        assert [x["member"] for x in d["cells"]] == ["eva"]
        assert d["outputs"] == []
        # Nothing of ace's (workspace B) may leak. Checked by ace's turn ids, not the
        # substring "ace": random uuids contain it ("…dbace44…") and flaked CI.
        ace_ids = {str(t) for t in Turn.objects.filter(agent__slug="ace").values_list("id", flat=True)}
        assert ace_ids and not any(i in str(d) for i in ace_ids)
        assert "worked_on" not in str(d["cells"])
    assert c.get("/api/w/connect/huddles/").json()[0]["outcome_count"] == 0
    assert c.get("/api/w/dimagi/huddles/h1").status_code in (403, 404)
