"""Session.activity — what a session DID, folded at ingest (board task T75).

Selecting "every session that built connect-labs' supply_chain" used to mean
rebuilding the signal from local Claude transcripts. These pin the index that
replaces that: repos, cwd, every branch seen, PRs, edited dirs, MCP tool counts —
and that it neither double-counts on re-ships nor loses runner-only context.
"""
import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client

from apps.canopy_sessions import activity, services
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership
from canopy_transcript import conversational_messages, stream_event

WT = "/Users/jj/emdash/worktrees/hal-8ac55e86/emdash-t75-abcde"
LABS = "/Users/jj/emdash/repositories/connect-labs"


def _bash(i, cmd):
    return {"role": "tool_use", "text": "", "content": {"id": f"t{i}", "name": "Bash",
                                                        "input": {"command": cmd}}}


def _result(i, text, error=False):
    return {"role": "tool_result", "text": text,
            "content": {"tool_use_id": f"t{i}", "is_error": error}}


def _fold(rows, data=None):
    act = activity.Activity(data)
    for r in rows:
        act.context(r.get("cwd"), r.get("git_branch"))
        act.row(r["role"], r.get("text", ""), r.get("content"))
    return act.d


# --- the fold itself ----------------------------------------------------------

def test_counts_mcp_tools_and_edited_dirs_per_repo():
    d = _fold([
        {"role": "tool_use", "content": {"id": "a", "name": "mcp__connect_labs__pipeline_sql", "input": {}}},
        {"role": "tool_use", "content": {"id": "b", "name": "mcp__connect_labs__pipeline_sql", "input": {}}},
        {"role": "tool_use", "content": {"id": "c", "name": "Edit", "input": {
            "file_path": f"{LABS}/connect_labs/supply_chain/models.py"}}},
        {"role": "tool_use", "content": {"id": "d", "name": "Write", "input": {
            "file_path": f"{WT}/skills/turn/SKILL.md"}}},
        {"role": "tool_use", "content": {"id": "e", "name": "Read", "input": {"file_path": "/etc/x"}}},
    ])
    assert d["mcp_tools"] == {"mcp__connect_labs__pipeline_sql": 2}
    assert d["paths"] == {"connect-labs:connect_labs/supply_chain": 1, "hal:skills/turn": 1}
    assert d["repos"] == ["connect-labs", "hal"]


def test_every_branch_and_cwd_seen_not_just_the_first():
    d = _fold([
        {"role": "assistant", "text": "a", "cwd": WT, "git_branch": "main"},
        {"role": "assistant", "text": "b", "cwd": WT, "git_branch": "hal/one"},
        {"role": "assistant", "text": "c", "cwd": f"{LABS}", "git_branch": "hal/two"},
        {"role": "assistant", "text": "d", "cwd": WT, "git_branch": "HEAD"},
    ])
    assert d["branches"] == ["main", "hal/one", "hal/two"]   # HEAD is not a branch
    assert d["cwds"] == [WT, LABS] and d["cwd"] == WT
    assert d["repos"] == ["hal", "connect-labs"]


def test_pr_create_pairs_with_its_result_across_batches():
    act = activity.Activity()
    act.row("tool_use", "", _bash(1, 'cd ~/emdash/repositories/connect-labs && gh pr create --title x --body-file b.md')["content"])
    assert act.d["pending"]   # the result has not arrived yet
    act2 = activity.Activity(act.d)   # next batch, re-read from the row
    act2.row("tool_result", "https://github.com/dimagi-internal/connect-labs/pull/2394\n",
             {"tool_use_id": "t1"})
    d = act2.d
    assert d["prs"] == [{"repo": "dimagi-internal/connect-labs", "number": 2394,
                         "url": "https://github.com/dimagi-internal/connect-labs/pull/2394",
                         "actions": ["created"]}]
    assert "pending" not in d
    assert d["remotes"] == ["dimagi-internal/connect-labs"]


def test_pr_merge_records_a_merge_request_and_joins_the_created_entry():
    d = _fold([
        _bash(1, "gh pr create -R dimagi-internal/canopy-web --fill"),
        _result(1, "https://github.com/dimagi-internal/canopy-web/pull/1390"),
        _bash(2, "gh pr merge 1390 -R dimagi-internal/canopy-web --auto"),
        _result(2, "! The merge strategy for main is set by the merge queue"),
        _bash(3, "gh pr merge https://github.com/dimagi-internal/ace/pull/7"),
        _result(3, "X Pull request is not mergeable", error=True),   # failed: not recorded
    ])
    assert d["prs"] == [{"repo": "dimagi-internal/canopy-web", "number": 1390,
                         "url": "https://github.com/dimagi-internal/canopy-web/pull/1390",
                         "actions": ["created", "merge"]}]


def test_push_output_yields_remote_and_branch_and_checkout_b_yields_branch():
    d = _fold([
        _bash(1, "git checkout -b hal/t75 origin/main && git push -u origin hal/t75"),
        _result(1, "remote: Create a pull request for 'hal/t75' on GitHub by visiting:\n"
                   "remote:      https://github.com/dimagi-internal/canopy-web/pull/new/hal/t75\n"
                   "To github.com:dimagi-internal/canopy-web.git\n"
                   " * [new branch]      hal/t75 -> hal/t75\n"),
        _bash(2, 'git worktree add "$WT" origin/main -b hal/other-$(date +%m%d)'),   # not literal: skipped
    ])
    assert d["branches"] == ["hal/t75"]
    assert d["remotes"] == ["dimagi-internal/canopy-web"]
    assert "canopy-web" in d["repos"]


# --- ingest -------------------------------------------------------------------

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, owner=user)
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a",
                               metadata={services.TRANSCRIPT_SOURCED: True})
    RunnerBinding.objects.create(session=s, runner=runner, session_key="hal-1")
    c = Client(); c.force_login(user)
    return user, ws, runner, s, c


def _events():
    return [
        {"kind": "tool_use", "seq": 64, "index": 64, "cwd": WT, "git_branch": "main",
         "payload": {"id": "t1", "name": "mcp__connect_labs__pipeline_sql", "input": {}, "text": ""}},
        {"kind": "tool_use", "seq": 128, "index": 128, "cwd": WT, "git_branch": "hal/t75",
         "payload": {"id": "t2", "name": "Bash", "text": "",
                     "input": {"command": "gh pr create -R dimagi-internal/connect-labs --fill"}}},
        {"kind": "tool_result", "seq": 192, "index": 192, "cwd": WT, "git_branch": "hal/t75",
         "payload": {"tool_use_id": "t2", "is_error": False,
                     "text": "https://github.com/dimagi-internal/connect-labs/pull/12"}},
    ]


def _stream(c, runner, s, events, transcript_id="tx1"):
    r = c.post(f"/api/harness/runners/{runner.id}/session-stream",
               {"session_id": str(s.id), "events": events, "transcript_id": transcript_id},
               content_type="application/json")
    assert r.status_code == 200, r.content


def test_stream_ingest_folds_activity_and_reships_do_not_double_count():
    user, ws, runner, s, c = _ctx()
    _stream(c, runner, s, _events())
    _stream(c, runner, s, _events())   # routine re-ship: rows already held
    s.refresh_from_db()
    a = s.activity
    assert a["mcp_tools"] == {"mcp__connect_labs__pipeline_sql": 1}
    assert a["branches"] == ["main", "hal/t75"]
    assert a["cwd"] == WT and a["repos"][0] == "hal"
    assert a["prs"][0]["number"] == 12 and a["prs"][0]["actions"] == ["created"]
    # Context is never written into the stored row.
    assert "cwd" not in Message.objects.get(session=s, turn_index=64).content


def test_backfill_carries_context_and_a_transcript_change_recounts():
    user, ws, runner, s, c = _ctx()
    _stream(c, runner, s, _events(), transcript_id="tx1")
    msgs = [{"role": e["kind"], "index": e["index"], "text": e["payload"].get("text", ""),
             "content": e["payload"], "cwd": LABS, "git_branch": "hal/other"}
            for e in _events()]
    # A different transcript: rows are dropped and replaced — counts must not double.
    r = c.post(f"/api/harness/runners/{runner.id}/session-backfill",
               {"session_id": str(s.id), "messages": msgs, "transcript_id": "tx2"},
               content_type="application/json")
    assert r.status_code == 200, r.content
    s.refresh_from_db()
    assert s.activity["mcp_tools"] == {"mcp__connect_labs__pipeline_sql": 1}
    assert "hal/other" in s.activity["branches"] and "connect-labs" in s.activity["repos"]


def test_list_filters_and_session_out():
    user, ws, runner, s, c = _ctx()
    other = Session.objects.create(workspace=ws, created_by=user, title="other")
    _stream(c, runner, s, _events())

    def ids(q):
        r = c.get(f"/api/canopy-sessions/?state=all&{q}")
        assert r.status_code == 200, r.content
        return {row["id"] for row in r.json()}

    assert ids("repo=connect-labs") == {str(s.id)}
    assert ids("repo=dimagi-internal/connect-labs") == {str(s.id)}
    assert ids("repo=hal") == {str(s.id)}            # from the cwd alone
    assert ids("repo=ace") == set()
    assert ids("branch=hal/t75") == {str(s.id)}
    assert ids("branch=nope") == set()
    assert ids("pr=12") == {str(s.id)}
    assert ids("pr=dimagi-internal/connect-labs%2312") == {str(s.id)}
    assert ids("pr=https://github.com/dimagi-internal/connect-labs/pull/12") == {str(s.id)}
    assert ids("pr=dimagi-internal/ace%2312") == set()
    assert str(other.id) in ids("")
    assert c.get("/api/canopy-sessions/?pr=banana").status_code == 422
    # The cursor walk shares the filters (one helper), so it cannot drift.
    walk = c.get("/api/canopy-sessions/search?branch=hal/t75&repo=connect-labs").json()
    assert [r["id"] for r in walk["sessions"]] == [str(s.id)]

    row = next(r for r in c.get("/api/canopy-sessions/?state=all").json() if r["id"] == str(s.id))
    assert row["activity"]["prs"][0]["number"] == 12
    assert "pending" not in row["activity"]


def test_rebuild_command_backfills_from_stored_rows():
    user, ws, runner, s, c = _ctx()
    Message.objects.create(session=s, turn_index=1, role="tool_use", plaintext="",
                           content={"id": "t9", "name": "mcp__nova__get_app", "input": {}})
    Message.objects.create(session=s, turn_index=2, role="tool_use", plaintext="",
                           content={"id": "t1", "name": "Bash", "input": {
                               "command": "gh pr merge 44 -R dimagi-internal/ace"}})
    Message.objects.create(session=s, turn_index=3, role="tool_result",
                           plaintext="✓ Squashed and merged pull request dimagi-internal/ace#44",
                           content={"tool_use_id": "t1", "is_error": False})
    s.activity = {"cwd": WT, "cwds": [WT], "branches": ["main"], "repos": ["hal"]}
    s.save(update_fields=["activity"])
    call_command("rebuild_session_activity", "--all")
    call_command("rebuild_session_activity", "--all")   # idempotent
    s.refresh_from_db()
    assert s.activity["mcp_tools"] == {"mcp__nova__get_app": 1}
    assert s.activity["prs"] == [{"repo": "dimagi-internal/ace", "number": 44, "url": "",
                                  "actions": ["merge"]}]
    assert s.activity["branches"] == ["main"] and s.activity["cwd"] == WT   # context kept


# --- the runner side ----------------------------------------------------------

def test_rows_and_stream_events_carry_record_context():
    records = [{"type": "assistant", "cwd": WT, "gitBranch": "hal/t75",
                "message": {"content": [{"type": "text", "text": "hi"}]}},
               {"type": "assistant", "message": {"content": "no context"}}]
    rows = conversational_messages(records, -1)
    assert rows[0]["cwd"] == WT and rows[0]["git_branch"] == "hal/t75"
    assert "cwd" not in rows[1]
    ev = stream_event(rows[0])
    assert ev["cwd"] == WT and ev["git_branch"] == "hal/t75"
    assert "cwd" not in ev["payload"]
    assert "cwd" not in stream_event(rows[1])


# --- noise (prod backfill, 2026-10-09: `$r` repos, one "repo" per worktree topic) --

def test_unexpanded_shell_tokens_are_never_repos_remotes_or_branches():
    d = _fold([
        _bash(1, 'for r in ace hal; do cd ~/emdash/repositories/$r && git log; done'),
        _bash(2, 'gh pr list -R dimagi-internal/$r --json number'),
        _bash(3, 'cp -R skills/linkedin-mentions /tmp/x && grep -R connect_labs/supply_chain .'),
        _bash(4, 'git checkout -b "hal/$(date +%s)" && git switch -c feat/*'),
        {"role": "assistant", "text": "x", "cwd": WT, "git_branch": "`whoami`"},
    ])
    activity.normalize(d)
    assert d.get("remotes", []) == []
    assert d["repos"] == ["hal"]                    # from the cwd only
    assert d.get("branches", []) == []


def test_worktree_folders_map_to_their_repo():
    remotes = {"canopy-web", "connect-labs"}
    assert activity.canonical_repo("canopy-web-popup-escape", remotes) == "canopy-web"
    assert activity.canonical_repo("connect-labs-today-flake", remotes) == "connect-labs"
    assert activity.canonical_repo("cw-fixes", set()) == "canopy-web"          # alias
    assert activity.canonical_repo("ace-web-assert", set()) == "ace-web"       # longest known prefix
    assert activity.canonical_repo("eva-allowlist2", set()) == "eva"
    assert activity.canonical_repo("labs-sql-explorer", remotes) == ""         # unexplained, has remotes
    assert activity.canonical_repo("dimagi-brand-mcp", set()) == "dimagi-brand-mcp"   # no remotes: kept
    assert activity.canonical_repo("$r", set()) == ""


def test_store_normalizes_a_noisy_activity_and_rebuild_recomputes_row_derived_keys():
    user, ws, runner, s, c = _ctx()
    Message.objects.create(session=s, turn_index=1, role="tool_use", plaintext="", content={
        "id": "t1", "name": "Edit",
        "input": {"file_path": "/Users/jj/emdash/repositories/connect-labs-today-flake/connect_labs/supply_chain/x.py"}})
    # What the first extractor stored on prod for sessions like this one.
    s.activity = {"cwds": [WT], "cwd": WT, "branches": ["main", "$b"],
                  "repos": ["$r", "connect-labs-today-flake", "supply_chain"],
                  "remotes": ["dimagi-internal/connect-labs", "connect_labs/supply_chain"],
                  "paths": {"connect-labs-today-flake:connect_labs/supply_chain": 3}}
    s.save(update_fields=["activity"])
    call_command("rebuild_session_activity", "--all")
    s.refresh_from_db()
    a = s.activity
    assert a["repos"] == ["hal", "connect-labs"]
    assert "remotes" not in a or a["remotes"] == []    # row-derived: no gh/push rows -> none
    assert a["branches"] == ["main"]
    assert a["paths"] == {"connect-labs:connect_labs/supply_chain": 1}
    ids = {r["id"] for r in c.get("/api/canopy-sessions/?state=all&repo=supply_chain").json()}
    assert str(s.id) not in ids
    ids = {r["id"] for r in c.get("/api/canopy-sessions/?state=all&repo=connect-labs").json()}
    assert str(s.id) in ids
