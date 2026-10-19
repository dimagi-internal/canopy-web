"""The semantic half of KB search on PostgreSQL + pgvector: the HNSW index, the
filtered nearest-neighbour query, and hybrid search through the project's MCP tool.

Skipped on SQLite (the default test DB); CI's `Backend pytest (PostgreSQL)` job runs
it against a pgvector/pgvector service (config/settings/test_pg.py)."""
from __future__ import annotations

import io

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection

from apps.agents import kb_store, kb_vectors
from apps.agents.models import Agent, AgentProject
from apps.workspaces.models import Workspace
from tests import kb_fakes

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(connection.vendor != "postgresql", reason="pgvector needs PostgreSQL"),
]


@pytest.fixture()
def project(tmp_path, monkeypatch, settings):
    kb_fakes.install(monkeypatch, settings)
    monkeypatch.setitem(kb_vectors._extension, "checked", False)
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner, parent=dimagi)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=owner)
    p = AgentProject.objects.create(agent=eva, ext_id="P12", name="GiveWell Knowledge Base", scope="workspace",
                                    owner_workspace=connect, handle="givewell-kb", mcp_kind="kb",
                                    kb_key="k", kb_sha256="a" * 64)
    path = kb_fakes.build_kb(tmp_path / "kb.sqlite")
    monkeypatch.setattr(kb_store, "local_path", lambda proj: path)
    return p


def test_the_extension_and_the_hnsw_index_are_there():
    with connection.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'kb_passage_embedding_hnsw'")
        (indexdef,) = cur.fetchone()
    assert "hnsw" in indexdef and "halfvec_cosine_ops" in indexdef


def test_nothing_to_search_until_the_first_sync(project):
    assert kb_vectors.available()
    assert kb_vectors.search(project, "cholera") is None


def test_nearest_passages_by_meaning_filtered_by_type_and_date(project):
    kb_vectors.sync(project)

    near = kb_vectors.search(project, "inoculation shortage")
    assert near[0] == ("doc:ocv", 0)
    assert kb_vectors.search(project, "inoculation shortage", types=["page"])[0] == ("page:lead", 0)
    assert all(i != "doc:ocv" for i, _ in kb_vectors.search(project, "inoculation", since="2026-01-01"))


def test_another_projects_vectors_never_answer(project):
    kb_vectors.sync(project)
    other = AgentProject.objects.create(agent=project.agent, ext_id="P13", name="Other", scope="workspace",
                                        owner_workspace=project.owner_workspace, handle="other-kb")
    assert kb_vectors.search(other, "inoculation shortage") is None


def test_hybrid_search_finds_a_paraphrase_through_the_mcp_tool(project, monkeypatch):
    from apps.mcp.projects import _call

    kb_vectors.sync(project)
    monkeypatch.setattr("apps.mcp.projects.readable_project", lambda user, ws, h: project)
    question = "jab shortage"  # no word in common with the OCV document

    lexical = _call("connect", "givewell-kb", "search", {"query": question, "mode": "lexical"})
    hybrid = _call("connect", "givewell-kb", "search", {"query": question})

    assert lexical == []
    assert hybrid[0]["id"] == "doc:ocv" and hybrid[0]["url"] == "https://gw/ocv"


def test_a_failed_question_embedding_falls_back_to_lexical(project, monkeypatch):
    from apps.mcp.projects import _call

    kb_vectors.sync(project)
    monkeypatch.setattr("apps.contacts.embeddings._invoke", lambda *a: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr("apps.mcp.projects.readable_project", lambda user, ws, h: project)
    assert [h["id"] for h in _call("connect", "givewell-kb", "search", {"query": "cholera"})] == ["doc:ocv"]


def test_kb_embed_then_kb_eval(project, tmp_path):
    golden = tmp_path / "golden.json"
    golden.write_text('[{"q": "jab shortage", "kind": "paraphrase", "rel": ["doc:ocv"]},'
                      ' {"q": "lead", "kind": "keyword", "rel": ["page:lead"]}]')
    out = io.StringIO()
    call_command("kb_embed", "connect/givewell-kb", stdout=out)
    assert "vectors in step" in out.getvalue()

    out = io.StringIO()
    call_command("kb_eval", "connect/givewell-kb", "--golden", str(golden), stdout=out)
    lines = {tuple(line.split()[:2]): line for line in out.getvalue().splitlines()[2:]}
    assert "0/1" in lines[("lexical", "paraphrase")]
    assert lines[("hybrid", "paraphrase")].split()[2] == "1/1"
