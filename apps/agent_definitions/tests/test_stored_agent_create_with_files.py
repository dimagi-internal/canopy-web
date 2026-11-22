"""Creating a stored agent WITH its files in one call (`POST /api/agents/stored`).

What is pinned, each with its refusal:
- revision 1 is the factory's scaffold with the sent files laid over it, published, and
  the generated files are canopy's own whatever is sent;
- a file the policy refuses refuses the whole create (nothing is made), or with
  `drop_refused` is left out and named in `dropped` (the scaffold's file at that path stays);
- a bundle keeps the source's history under `refs/heads/imported`, after a secret scan of
  every blob; too much is 413;
- a taken slug is 409 `exists`; the gate is the editor's, as for any create.
"""
from __future__ import annotations

import base64
import json
import subprocess

import pytest

from apps.agent_definitions import services
from apps.agents import definition_git as git
from apps.agents import definition_store as store
from apps.agents.models import Agent, AgentSkill
from tests.fixtures.stored_agents import _browser, _post

pytestmark = pytest.mark.django_db

SKILL = "---\nname: patrol\ndescription: Walk the line.\n---\nGo.\n"


def _create(client, *, slug="guide", **body):
    return _post(client, "/api/agents/stored", {"name": "Guide", "slug": slug, "mandate": "Guides walkers.",
                                                "workspace": "scouts", **body})


def test_files_are_laid_over_the_scaffold_as_revision_1(w):
    r = _create(_browser(w["editor"]), message="Moved in", files=[
        {"path": "CLAUDE.md", "content": "# Guide\nOur own.\n"},
        {"path": "skills/patrol/SKILL.md", "content_b64": base64.b64encode(SKILL.encode()).decode()},
        {"path": ".claude-plugin/plugin.json", "content": '{"name": "guide", "hooks": {"x": 1}}'},
    ])
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["revision"] == 1 and body["dropped"] == [] and body["owner"] == "eddie@dimagi.com"
    agent = Agent.objects.get(slug="guide")
    rev = agent.live_revision
    assert rev.message == "Moved in" and rev.root_sha256 == body["root_sha256"]
    assert store.read_file(rev, "CLAUDE.md") == b"# Guide\nOur own.\n"
    assert store.read_file(rev, "skills/patrol/SKILL.md") == SKILL.encode()
    assert "persona.md" in rev.tree and "skills/turn/SKILL.md" in rev.tree  # the scaffold is under them
    plugin = json.loads(store.read_file(rev, ".claude-plugin/plugin.json"))
    assert "hooks" not in plugin and plugin["version"] == "0.1.0"  # canopy's own, not the upload
    assert AgentSkill.objects.filter(agent=agent, name="patrol").exists()


def test_a_files_text_is_kept_byte_for_byte_in_a_create_and_a_draft(w, agent):
    text = "  indented first line\n\nlast line\n\n"
    assert _create(_browser(w["editor"]), files=[{"path": "docs/notes.md", "content": text}]).status_code == 201
    assert store.read_file(Agent.objects.get(slug="guide").live_revision, "docs/notes.md") == text.encode()
    r = _post(_browser(w["owner"]), "/api/agents/scout/definition/drafts",
              {"base": 1, "changes": [{"path": "docs/notes.md", "content": text}], "message": "notes"})
    assert r.status_code == 201, r.content
    from apps.agents.models import AgentDefinitionRevision

    draft = AgentDefinitionRevision.objects.get(agent=agent, number=r.json()["number"])
    assert store.read_file(draft, "docs/notes.md") == text.encode()


def test_a_refused_file_refuses_the_whole_create(w):
    r = _create(_browser(w["editor"]), files=[{"path": "CLAUDE.md", "content": "x\n"},
                                              {"path": "hooks/hooks.json", "content": "{}"}])
    assert r.status_code == 422, r.content
    assert [p["path"] for p in r.json()["problems"]] == ["hooks/hooks.json"]
    assert not Agent.objects.filter(slug="guide").exists()


def test_drop_refused_leaves_them_out_and_names_them(w):
    r = _create(_browser(w["editor"]), drop_refused=True, files=[
        {"path": "CLAUDE.md", "content": "# Guide\n"},
        {"path": "hooks/hooks.json", "content": "{}"},
        {"path": "scripts/x.py", "content": "print()"},
        {"path": "persona.md", "content": "token: ghp_" + "a" * 36},
    ])
    assert r.status_code == 201, r.content
    assert r.json()["dropped"] == ["hooks/hooks.json", "persona.md", "scripts/x.py"]
    rev = Agent.objects.get(slug="guide").live_revision
    assert store.read_file(rev, "CLAUDE.md") == b"# Guide\n"
    assert "hooks/hooks.json" not in rev.tree and "scripts/x.py" not in rev.tree
    assert b"ghp_" not in store.read_file(rev, "persona.md")  # the scaffold's persona stays


def test_a_cap_on_the_whole_tree_is_never_dropped_around(w):
    r = _create(_browser(w["editor"]), drop_refused=True,
                files=[{"path": f"docs/d{i}.md", "content": "y\n"} for i in range(201)])
    assert r.status_code == 422 and "too_many_files" in [p["code"] for p in r.json()["problems"]]
    assert not Agent.objects.filter(slug="guide").exists()


@pytest.mark.parametrize("bad", [{"path": "CLAUDE.md"}, {"path": "CLAUDE.md", "content": "x", "content_b64": "eA=="},
                                 {"path": "CLAUDE.md", "content_b64": "%%%"}])
def test_each_file_is_content_or_content_b64(w, bad):
    assert _create(_browser(w["editor"]), files=[bad]).status_code == 422
    assert not Agent.objects.filter(slug="guide").exists()


def test_a_taken_slug_is_exists(w, agent):
    r = _create(_browser(w["editor"]), slug="scout", files=[{"path": "CLAUDE.md", "content": "x\n"}])
    assert r.status_code == 409 and r.json()["error"] == "exists"


def test_the_gate_is_an_editors(w):
    assert _create(_browser(w["viewer"]), files=[{"path": "CLAUDE.md", "content": "x\n"}]).status_code == 403
    assert _create(_browser(w["outsider"])).status_code == 404


# --- history --------------------------------------------------------------------------


def _git(*args, cwd):
    return subprocess.run(["git", "-c", "user.email=g@g", "-c", "user.name=Guide", *args], cwd=cwd, check=True,
                          capture_output=True).stdout


def _bundle(tmp_path, *, leak: str = "") -> tuple[bytes, str]:
    src = tmp_path / "guide"
    src.mkdir()
    _git("init", "-q", "-b", "main", cwd=src)
    (src / "CLAUDE.md").write_text("# Guide v1\n" + (f"token: {leak}\n" if leak else ""))
    _git("add", "-A", cwd=src)
    _git("commit", "-qm", "first", cwd=src)
    (src / "CLAUDE.md").write_text("# Guide v2\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-qm", "second", cwd=src)
    return _git("bundle", "create", "-", "main", cwd=src), _git("rev-parse", "main", cwd=src).decode().strip()


def test_a_bundle_keeps_the_history_under_imported(w, tmp_path):
    bundle, tip = _bundle(tmp_path)
    r = _create(_browser(w["editor"]), files=[{"path": "CLAUDE.md", "content": "# Guide v2\n"}],
                bundle_b64=base64.b64encode(bundle).decode())
    assert r.status_code == 201, r.content
    agent = Agent.objects.get(slug="guide")
    refs = git.refs_of(agent.pk)
    assert refs[git.IMPORTED] == tip
    commit = store.object_store(agent)[agent.live_revision.commit.encode()]
    assert [p.decode() for p in commit.parents] == [tip]


def test_a_bundle_with_a_secret_in_its_history_is_refused(w, tmp_path):
    bundle, _ = _bundle(tmp_path, leak="ghp_" + "b" * 36)
    r = _create(_browser(w["editor"]), files=[{"path": "CLAUDE.md", "content": "# Guide v2\n"}],
                bundle_b64=base64.b64encode(bundle).decode())
    assert r.status_code == 422 and r.json()["error"] == "secret"
    assert not Agent.objects.filter(slug="guide").exists()


def test_too_much_is_413(w, tmp_path, monkeypatch):
    bundle, _ = _bundle(tmp_path)
    monkeypatch.setattr(services, "MAX_CONVERT_BYTES", len(bundle))
    r = _create(_browser(w["editor"]), files=[{"path": "CLAUDE.md", "content": "x\n"}],
                bundle_b64=base64.b64encode(bundle).decode())
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    assert not Agent.objects.filter(slug="guide").exists()


def test_the_mcp_tool_takes_the_files():
    from apps.api.api import api
    from apps.mcp import api_tools

    assert api_tools.tool_operation("POST", "/api/agents/stored") == "create_stored_agent"
    fields = api.get_openapi_schema()["components"]["schemas"]["StoredAgentIn"]["properties"]
    assert {"files", "bundle_b64", "drop_refused", "message"} <= set(fields)
