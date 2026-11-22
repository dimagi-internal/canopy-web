"""Stored agents: a definition canopy keeps in revisions, changed only through the policy.

What is pinned, each with its refusal:
- creating one stamps the factory into revision 1, published, with only allowed paths;
- every write goes through the file policy (hooks, generated paths, executables,
  traversal, secrets, caps), and generated files are always canopy's own;
- an agent may draft its own definition, which files an approval; it may never publish,
  roll back or convert — nor may any turn's token;
- publishing supersedes, rollback appends, history is never rewritten;
- the archive is what the manifest root names, and only a box that may hold the agent
  (or a member) gets it — a box never gets a draft.
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import tarfile
import uuid

import pytest
from canopy_sdk import agent_definition as sdk

from apps.agent_definitions import services
from apps.agents import definition_store as store
from apps.agents.models import Agent, AgentAdmin, AgentDefinitionRevision, AgentSkill
from apps.approvals import services as approvals
from apps.approvals.models import ApprovalRequest
from apps.common import redact
from apps.harness.models import Turn
from tests.token_helpers import pat_client

from tests.fixtures.stored_agents import (
    SLUG,
    _browser,
    _create,
    _draft,
    _files,
    _post,
    _runner_client,
)

pytestmark = pytest.mark.django_db

# --- creating ---------------------------------------------------------------


def test_creating_a_stored_agent_publishes_the_factory_as_revision_1(w, agent):
    assert agent.source == Agent.STORED and agent.repo_url == "" and agent.owner == w["owner"]
    rev = agent.live_revision
    assert rev.number == 1 and rev.status == AgentDefinitionRevision.PUBLISHED
    paths = set(rev.tree)
    assert {"CLAUDE.md", "persona.md", "README.md", "config/agent.json", "skills/turn/SKILL.md"} <= paths
    assert sdk.generated_paths(SLUG) <= paths
    # Nothing a repo agent needs for GitHub: no workflows, scripts, Python, hooks.
    assert not any(p.startswith((".github/", "scripts/", "hooks/")) or p.endswith(".py") for p in paths)
    assert [p for p, e in rev.tree.items() if e["executable"]] == [f"bin/{SLUG}-email"]
    assert rev.root_sha256 == sdk.manifest_root(rev.tree)
    # The whole tree passes canopy's own check.
    files = [{"path": p, "content": d, "executable": x} for p, (d, x) in store.files_of(rev).items()]
    assert sdk.check_tree(files, slug=SLUG, allow_generated=True) == []
    # Its skills are its catalog, read from the revision.
    assert AgentSkill.objects.filter(agent=agent, name="turn").exists()


def test_generated_files_say_nothing_but_canopys_own(agent):
    rev = agent.live_revision
    plugin = json.loads(store.read_file(rev, ".claude-plugin/plugin.json"))
    assert plugin["name"] == SLUG and plugin["version"] == "0.1.0"
    assert "hooks" not in plugin and "mcpServers" not in plugin
    market = json.loads(store.read_file(rev, ".claude-plugin/marketplace.json"))
    assert market["plugins"][0]["source"] == "./"
    assert json.loads(store.read_file(rev, ".claude/settings.json")) == {"env": {"CANOPY_AGENT": SLUG}}


def test_create_gate(w, agent):
    assert _create(_browser(w["viewer"]), slug="other").status_code == 403
    assert _create(_browser(w["outsider"]), slug="other").status_code == 404
    assert _create(_browser(w["editor"]), slug=SLUG).status_code == 409
    assert _create(_browser(w["editor"]), slug="stored").status_code == 422


def test_an_agents_login_creates_for_a_person_never_itself(w, agent):
    r = _create(pat_client(w["bot"]), slug="other")
    assert r.status_code == 201, r.content
    assert Agent.objects.get(slug="other").owner == w["owner"]
    # Its turn's asker, when it names a live turn of its own that a person asked for.
    turn = Turn.objects.create(agent=agent, origin="api", prompt="make one", idempotency_key="t-own",
                               status=Turn.RUNNING, initiator_kind="user", initiator_user=w["editor"],
                               initiator_assurance="session")
    r = _create(pat_client(w["bot"], HTTP_X_CANOPY_PARENT_TURN=str(turn.pk)), slug="third")
    assert r.status_code == 201, r.content
    assert Agent.objects.get(slug="third").owner == w["editor"]


# --- reading ----------------------------------------------------------------


def test_agent_detail_names_a_stored_agents_source(w, agent):
    detail = _browser(w["viewer"]).get(f"/api/agents/{SLUG}/").json()
    assert detail["source"] == "stored" and detail["definition"]["source"] == "stored"


def test_reading_a_definition(w, agent):
    c = _browser(w["viewer"])
    d = c.get(f"/api/agents/{SLUG}/definition").json()
    assert d["source"] == "stored" and d["live"]["number"] == 1 and d["draft"] is None
    assert d["policy"]["max_files"] == 200 and d["can_publish"] is False
    one = c.get(f"/api/agents/{SLUG}/definition/revisions/1").json()
    assert one["root_sha256"] == agent.live_revision.root_sha256 and "CLAUDE.md" in one["tree"]
    f = c.get(f"/api/agents/{SLUG}/definition/revisions/1/files/persona.md")
    assert f.status_code == 200 and f["Content-Type"].startswith("text/plain")
    assert c.get(f"/api/agents/{SLUG}/definition/revisions/1/files/nope.md").status_code == 404
    assert _browser(w["outsider"]).get(f"/api/agents/{SLUG}/definition").status_code == 404
    assert _browser(w["owner"]).get(f"/api/agents/{SLUG}/definition").json()["can_publish"] is True


# --- drafts and the policy --------------------------------------------------


def test_a_persons_draft_waits_for_publish_and_asks_nobody(w, agent):
    r = _draft(_browser(w["editor"]))
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["status"] == "draft" and body["number"] == 2 and body["approval_id"] is None
    agent.refresh_from_db()
    assert agent.live_revision.number == 1  # nothing went live
    diff = _browser(w["viewer"]).get(f"/api/agents/{SLUG}/definition/revisions/2/diff").json()
    edited = [f for f in diff["files"] if f["path"] not in sdk.generated_paths(SLUG)]
    assert [f["path"] for f in edited] == ["skills/trail/SKILL.md"]
    assert diff["against"] == 1 and "+Walk it." in edited[0]["diff"]
    # The version canopy writes moves with the revision, and the diff says so.
    assert ".claude-plugin/plugin.json" in [f["path"] for f in diff["files"]]


def test_a_second_draft_replaces_the_first(w, agent):
    c = _browser(w["editor"])
    assert _draft(c).status_code == 201
    r = _draft(c, changes=[{"path": "persona.md", "content": "A scout.\n"}])
    assert r.status_code == 201 and r.json()["number"] == 3
    assert AgentDefinitionRevision.objects.get(agent=agent, number=2).status == "discarded"
    assert AgentDefinitionRevision.objects.filter(agent=agent, status="draft").count() == 1


def test_a_stale_base_is_refused(w, agent):
    r = _draft(_browser(w["editor"]), base=7)
    assert r.status_code == 409 and r.json()["error"] == "stale_base"


@pytest.mark.parametrize("change,code", [
    ({"path": "hooks/hooks.json", "content": "{}"}, "hooks"),
    ({"path": ".claude-plugin/plugin.json", "content": '{"mcpServers": {}}'}, "server_generated"),
    ({"path": ".claude/settings.json", "content": "{}"}, "server_generated"),
    ({"path": f"bin/{SLUG}-email", "content": "#!/bin/sh\ncurl evil\n"}, "server_generated"),
    ({"path": ".mcp.json", "content": "{}"}, "not_editable"),
    ({"path": "bin/run.sh", "content": "#!/bin/sh\n", "executable": True}, "not_editable"),
    ({"path": "skills/x/SKILL.md", "content": "x", "executable": True}, "executable"),
    ({"path": "../escape.md", "content": "x"}, "bad_path"),
    ({"path": "/etc/passwd", "content": "x"}, "bad_path"),
    ({"path": "skills\\x\\SKILL.md", "content": "x"}, "bad_path"),
    ({"path": ".git/config", "content": "x"}, "bad_path"),
    ({"path": "skills/x/SKILL.md", "content": "key: ghp_" + "a" * 36}, "secret"),
    ({"path": "skills/x/SKILL.md", "content": "x" * (sdk.MAX_FILE_BYTES + 1)}, "too_large"),
    ({"path": "skills/x/SKILL.md", "content_b64": base64.b64encode(b"\xff\xfe").decode()}, "not_utf8"),
])
def test_the_policy_refuses_every_write_outside_it(w, agent, change, code):
    r = _draft(_browser(w["owner"]), changes=[change])
    assert r.status_code == 422, r.content
    assert code in [p["code"] for p in r.json()["problems"]]
    assert not AgentDefinitionRevision.objects.filter(agent=agent, status="draft").exists()


def test_a_symlink_cannot_even_be_described(w, agent):
    r = _draft(_browser(w["owner"]), changes=[{"path": "skills/x/SKILL.md", "content": "x", "symlink": True}])
    assert r.status_code == 422


def test_the_tree_caps_hold_across_a_draft(w, agent):
    chunk = "y" * (200 * 1024)
    many = [{"path": f"docs/d{i}.md", "content": chunk} for i in range(11)]
    r = _draft(_browser(w["owner"]), changes=many)
    assert r.status_code == 422 and "tree_too_large" in [p["code"] for p in r.json()["problems"]]


def test_drafting_needs_agent_edit(w, agent):
    assert _draft(_browser(w["viewer"])).status_code == 403
    assert _draft(_browser(w["outsider"])).status_code == 404


# --- an agent drafts itself; a person publishes -----------------------------


def test_an_agents_own_draft_files_an_approval_and_publishes_nothing(w, agent):
    turn = Turn.objects.create(agent=agent, origin="api", prompt="improve", idempotency_key="t1",
                               status=Turn.RUNNING)
    r = _draft(pat_client(w["bot"]), HTTP_X_CANOPY_PARENT_TURN=str(turn.pk))
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["author"]["by_agent"] is True and body["author"]["turn_id"] == str(turn.pk)
    req = ApprovalRequest.objects.get(pk=body["approval_id"])
    assert req.kind == "agent.definition.publish" and req.requested_by_agent == agent
    assert req.params == {"agent": SLUG, "revision": 2, "root_sha256": body["root_sha256"]}
    agent.refresh_from_db()
    assert agent.live_revision.number == 1


@pytest.mark.parametrize("who", ["bot_pat", "turn_header", "owner_turn_header"])
def test_no_agent_and_no_turn_may_publish_roll_back_or_convert(w, agent, who):
    assert _draft(_browser(w["editor"])).status_code == 201
    turn = Turn.objects.create(agent=agent, origin="api", prompt="x", idempotency_key="t2", status=Turn.RUNNING)
    if who == "bot_pat":
        c, extra = pat_client(w["bot"]), {}
    else:
        user = w["bot"] if who == "turn_header" else w["owner"]
        c, extra = pat_client(user), {"HTTP_X_CANOPY_TURN_ID": str(turn.pk)}
    r = _post(c, f"/api/agents/{SLUG}/definition/revisions/2/publish", **extra)
    assert r.status_code == 403, r.content
    r = _post(c, f"/api/agents/{SLUG}/definition/rollback", {"revision": 1}, **extra)
    assert r.status_code == 403
    agent.refresh_from_db()
    assert agent.live_revision.number == 1


def test_an_editor_who_is_not_an_admin_cannot_publish(w, agent):
    assert _draft(_browser(w["editor"])).status_code == 201
    assert _post(_browser(w["editor"]), f"/api/agents/{SLUG}/definition/revisions/2/publish").status_code == 403


@pytest.mark.parametrize("client_of", [_browser, pat_client])
def test_an_admin_publishes_and_the_old_live_is_superseded(w, agent, client_of):
    assert _draft(_browser(w["editor"])).status_code == 201
    r = _post(client_of(w["admin"]), f"/api/agents/{SLUG}/definition/revisions/2/publish")
    assert r.status_code == 200, r.content
    agent.refresh_from_db()
    live = agent.live_revision
    assert live.number == 2 and live.published_by == w["admin"]
    assert AgentDefinitionRevision.objects.get(agent=agent, number=1).status == "superseded"
    # Generated files are rewritten for the revision that went live.
    assert json.loads(store.read_file(live, ".claude-plugin/plugin.json"))["version"] == "0.2.0"
    assert live.root_sha256 == sdk.manifest_root(live.tree)
    # The catalog follows the live revision.
    assert AgentSkill.objects.filter(agent=agent, name="trail").exists()
    # Publishing twice is a conflict, not a second publish.
    assert _post(client_of(w["admin"]), f"/api/agents/{SLUG}/definition/revisions/2/publish").status_code == 409


def test_approving_an_agents_draft_publishes_exactly_that_draft(w, agent):
    r = _draft(pat_client(w["bot"]))
    req_id = r.json()["approval_id"]
    # The agent's own login is no approver, and a person approves only with a passkey.
    with pytest.raises(approvals.ApprovalError):
        approvals.decide(req_id, w["bot"], "approve", stepped_up=True)
    with pytest.raises(approvals.ApprovalError):
        approvals.decide(req_id, w["owner"], "approve")
    req = approvals.decide(req_id, w["owner"], "approve", stepped_up=True)
    assert req.status == ApprovalRequest.DONE, req.error
    agent.refresh_from_db()
    assert agent.live_revision.number == 2 and agent.live_revision.published_by == w["owner"]


def test_a_replaced_draft_cannot_be_published_by_its_old_approval(w, agent):
    first = _draft(pat_client(w["bot"])).json()["approval_id"]
    assert _draft(pat_client(w["bot"]), changes=[{"path": "persona.md", "content": "Other.\n"}]).status_code == 201
    assert ApprovalRequest.objects.get(pk=first).status == ApprovalRequest.CANCELLED
    with pytest.raises(approvals.ApprovalError):
        approvals.decide(first, w["owner"], "approve", stepped_up=True)
    agent.refresh_from_db()
    assert agent.live_revision.number == 1


def test_discard(w, agent):
    rid = _draft(pat_client(w["bot"])).json()["approval_id"]
    r = _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/revisions/2/discard")
    assert r.status_code == 200 and r.json()["status"] == "discarded"
    assert ApprovalRequest.objects.get(pk=rid).status == ApprovalRequest.CANCELLED
    assert _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/revisions/1/discard").status_code == 409


def test_rollback_appends_a_new_revision(w, agent):
    assert _draft(_browser(w["editor"])).status_code == 201
    assert _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/revisions/2/publish").status_code == 200
    r = _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/rollback", {"revision": 1})
    assert r.status_code == 201, r.content
    agent.refresh_from_db()
    live = agent.live_revision
    assert live.number == 3 and "skills/trail/SKILL.md" not in live.tree
    one = AgentDefinitionRevision.objects.get(agent=agent, number=1)
    editable = {p: e for p, e in one.tree.items() if p not in sdk.generated_paths(SLUG)}
    assert {p: e for p, e in live.tree.items() if p not in sdk.generated_paths(SLUG)} == editable
    assert json.loads(store.read_file(live, ".claude-plugin/plugin.json"))["version"] == "0.3.0"
    assert [r.status for r in AgentDefinitionRevision.objects.filter(agent=agent).order_by("number")] == [
        "superseded", "superseded", "published"]
    assert _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/rollback",
                 {"revision": 9}).status_code == 409


# --- converting a repo agent ------------------------------------------------


def test_convert_turns_a_repo_agent_into_a_stored_one(w, repo_agent):
    files = _files(**{"CLAUDE.md": "# Ranger\n", "skills/patrol/SKILL.md": "---\nname: patrol\n---\nGo.\n",
                      ".claude-plugin/plugin.json": '{"name": "ranger", "hooks": {"x": 1}}'})
    r = _post(_browser(w["owner"]), "/api/agents/ranger/definition/convert", {"files": files, "message": "move"})
    assert r.status_code == 201, r.content
    repo_agent.refresh_from_db()
    assert repo_agent.source == "stored" and repo_agent.repo_url == "" and repo_agent.live_revision.number == 1
    plugin = json.loads(store.read_file(repo_agent.live_revision, ".claude-plugin/plugin.json"))
    assert "hooks" not in plugin  # the uploaded manifest was ignored and rewritten
    again = _post(_browser(w["owner"]), "/api/agents/ranger/definition/convert", {"files": files})
    assert again.status_code == 409


def test_convert_refuses_anything_outside_the_policy_and_changes_nothing(w, repo_agent):
    files = _files(**{"CLAUDE.md": "# Ranger\n", "hooks/hooks.json": "{}", "scripts/x.py": "print()"})
    r = _post(_browser(w["owner"]), "/api/agents/ranger/definition/convert", {"files": files})
    assert r.status_code == 422
    assert {p["path"] for p in r.json()["problems"]} == {"hooks/hooks.json", "scripts/x.py"}
    repo_agent.refresh_from_db()
    assert repo_agent.source == "repo" and repo_agent.repo_url


def test_convert_is_a_persons_act_by_an_admin(w, repo_agent):
    files = _files(**{"CLAUDE.md": "x\n"})
    assert _post(_browser(w["editor"]), "/api/agents/ranger/definition/convert", {"files": files}).status_code == 403
    repo_agent.user = w["bot"]
    repo_agent.save(update_fields=["user"])
    AgentAdmin.objects.create(agent=repo_agent, user=w["bot"], granted_by=w["owner"])
    assert _post(pat_client(w["bot"]), "/api/agents/ranger/definition/convert", {"files": files}).status_code == 403
    assert _post(pat_client(w["owner"]), "/api/agents/ranger/definition/convert", {"files": files},
                 HTTP_X_CANOPY_TURN_ID=str(uuid.uuid4())).status_code == 403


def test_a_thirty_file_convert_fits_in_one_request(w, repo_agent):
    files = _files(**{f"skills/s{i}/SKILL.md": f"---\nname: s{i}\n---\n" + "words " * 1500 for i in range(30)})
    r = _post(_browser(w["owner"]), "/api/agents/ranger/definition/convert", {"files": files})
    assert r.status_code == 201, r.content


# --- the archive --------------------------------------------------------------


def _untar(data: bytes) -> list[dict]:
    out = []
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tar:
        for m in tar.getmembers():
            assert m.isfile() and m.mode in (0o644, 0o755)
            out.append({"path": m.name, "content": tar.extractfile(m).read(), "executable": m.mode == 0o755})
    return out


def test_the_archive_is_what_the_manifest_root_names(w, agent):
    r = _browser(w["viewer"]).get(f"/api/agents/{SLUG}/definition/archive")
    assert r.status_code == 200 and r["Content-Type"] == "application/gzip"
    assert r["X-Canopy-Revision"] == "1" and r["X-Canopy-Root-Sha256"] == agent.live_revision.root_sha256
    assert r["X-Canopy-Published-At"].endswith("Z") and r["X-Canopy-Workspace"] == "scouts"
    files = _untar(r.content)
    assert sdk.manifest_root_of_files(files) == r["X-Canopy-Root-Sha256"]
    # Deterministic: the same revision is the same bytes.
    assert _browser(w["owner"]).get(f"/api/agents/{SLUG}/definition/archive?revision=1").content == r.content
    assert _browser(w["outsider"]).get(f"/api/agents/{SLUG}/definition/archive").status_code == 404


def test_a_box_that_may_hold_the_agent_fetches_its_published_revisions_only(w, agent):
    box, _ = _runner_client(w["owner"], w["ws"], agent)
    r = box.get(f"/api/agents/{SLUG}/definition/archive")
    assert r.status_code == 200, r.content
    assert r["X-Canopy-Revision"] == "1"
    assert _draft(_browser(w["editor"])).status_code == 201
    assert box.get(f"/api/agents/{SLUG}/definition/archive?revision=2").status_code == 404
    # A box reaches nothing else of the definition API.
    assert box.get(f"/api/agents/{SLUG}/definition").status_code == 403


def test_a_box_whose_owner_does_not_administer_the_agent_gets_nothing(w, agent):
    box, _ = _runner_client(w["editor"], w["ws"], agent)
    assert box.get(f"/api/agents/{SLUG}/definition/archive").status_code == 404


def test_the_archive_of_a_repo_agent_is_a_conflict(w, repo_agent):
    assert _browser(w["owner"]).get("/api/agents/ranger/definition/archive").status_code == 409


# --- the two lists of secret shapes agree -------------------------------------


@pytest.mark.parametrize("sample", [
    "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
    "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0In0" + ".c2lnbmF0dXJlc2ln",
    "sk-" + "a" * 30, "cpat_" + "b" * 30, "cact_" + "c" * 30, "zks1:" + "d" * 30, "ya29." + "e" * 30,
    "https://user:pa55word@host/x", "Authorization: Basic dXNlcjpwYXNzd29yZA==", "ghp_" + "f" * 36,
    "github_pat_" + "g" * 30, "AK" + "IAABCDEFGHIJKLMNOP", "AIza" + "h" * 35, "xoxb-" + "1" * 20,
    "Authorization: Bearer " + "i" * 30,
])
def test_every_shape_the_transcript_scrubber_knows_is_refused_in_a_definition(sample):
    assert redact.redact_text(sample)[1] > 0
    assert sdk.secret_kinds(sample)


def test_services_refuse_a_draft_on_a_repo_agent(w, repo_agent):
    with pytest.raises(services.Refused):
        services.create_draft(repo_agent, base=1, changes=[{"path": "CLAUDE.md", "content": b"x"}],
                              message="", by=w["owner"], by_agent=False)


# --- the rest of canopy reads a stored agent right ---------------------------


def test_agent_detail_runtime_and_list_name_the_stored_definition(w, agent):
    c = _browser(w["viewer"])
    d = c.get(f"/api/agents/{SLUG}/").json()["definition"]
    assert d["source"] == "stored" and d["revision"] == 1 and d["root_sha256"] == agent.live_revision.root_sha256
    assert d["key"] == f"canopy:scouts/{SLUG}" and d["shared_with"] == []
    rt = c.get(f"/api/agents/{SLUG}/runtime").json()
    assert rt["repo_url"] == "" and rt["definition"] == {
        "source": "stored", "revision": 1, "root_sha256": agent.live_revision.root_sha256,
        "commit": agent.live_revision.commit, "git_url": f"{store.git_url(agent)}"}
    assert rt["definition"]["git_url"].endswith(f"/git/scouts/{SLUG}.git")
    listed = {a["slug"]: a for a in c.get("/api/agents/?workspace=scouts", HTTP_X_CANOPY_WORKSPACE="scouts").json()["items"]}
    assert listed[SLUG]["source"] == "stored"


def test_a_repo_agent_reads_as_one(w, repo_agent):
    rt = _browser(w["owner"]).get("/api/agents/ranger/runtime").json()
    assert rt["definition"] == {"source": "repo", "revision": None, "root_sha256": None, "commit": None,
                                "git_url": None}
    assert store.source_of(repo_agent)["source"] == "repo"


def test_a_box_reads_the_definition_source_from_runtime(w, agent):
    box, _ = _runner_client(w["owner"], w["ws"], agent)
    assert box.get(f"/api/agents/{SLUG}/runtime").json()["definition"]["source"] == "stored"


def test_a_stored_agent_cannot_be_pointed_at_a_repository(w, agent):
    c = _browser(w["owner"])
    r = c.patch(f"/api/agents/{SLUG}/repository", data=json.dumps({"repo_url": "https://github.com/x/y"}),
              content_type="application/json")
    assert r.status_code == 409
    r = _post(c, "/api/agents/", {"slug": SLUG, "name": "Scout", "repo_url": "https://github.com/x/y"})
    assert r.status_code == 409
    agent.refresh_from_db()
    assert agent.repo_url == ""
    r = c.put(f"/api/agents/{SLUG}/skills/", data=json.dumps({"skills": []}), content_type="application/json")
    assert r.status_code == 409


def test_setup_has_no_repo_or_github_step(agent):
    from apps.agents.setup import setup_steps

    keys = [s["key"] for s in setup_steps(agent)]
    assert "repo" not in keys and "github" not in keys and keys[0] == "definition"


def test_no_plugin_catalog_entry_and_no_ship_grant(w, agent):
    from apps.harness import caller_context
    from apps.plugins import services as plugins

    assert agent not in plugins.member_agents(w["owner"])
    assert caller_context._agent_repo(agent) == ""
    turn = Turn.objects.create(agent=agent, origin="api", prompt="x", idempotency_key="t9")
    assert caller_context._ship_grant(turn, agent, "admin") is None


def test_skill_history_comes_from_published_revisions(w, agent):
    from apps.agents import skill_history

    assert _draft(_browser(w["editor"])).status_code == 201
    assert _post(_browser(w["owner"]), f"/api/agents/{SLUG}/definition/revisions/2/publish").status_code == 200
    skill_history.sync(agent, force=True)
    agent.refresh_from_db()
    payload = skill_history.history_payload(agent, w["owner"])
    assert payload["credential_state"] == "ok" and len(payload["commits"]) == 2
    assert "trail" in {s["name"] for s in payload["skills"]}
    sha = agent.live_revision.root_sha256
    diff = skill_history.revision_diff(agent, sha, "trail")
    assert "+Walk it." in diff["patch"]
