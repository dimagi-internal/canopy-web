"""A workspace project's KB vectors — DORMANT (KB_VECTORS off): that off means off, and,
for a corpus that turns them on, the sync that keeps them in step with the published
file and the fallback wherever they cannot be searched.

These run on SQLite, where the halfvec column is just a column and there is no
nearest-neighbour search; the search itself is covered on PostgreSQL by
tests/test_kb_vectors_pg.py (CI's `Backend pytest (PostgreSQL)` job)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import connection

from apps.agents import kb_rerank, kb_store, kb_vectors
from apps.agents.models import Agent, AgentProject, KbPassage
from apps.workspaces.models import Workspace
from tests import kb_fakes

pytestmark = pytest.mark.django_db
#: Asserts what is true only without vector search (CI also runs this file on PostgreSQL).
sqlite_only = pytest.mark.skipif(connection.vendor == "postgresql", reason="vector search is on here")


@pytest.fixture()
def project(tmp_path, monkeypatch):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner, parent=dimagi)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=owner)
    p = AgentProject.objects.create(agent=eva, ext_id="P12", name="GiveWell Knowledge Base", 
                                    workspace=connect, handle="givewell-kb", mcp_kind="kb",
                                    kb_key="k", kb_sha256="a" * 64)
    path = kb_fakes.build_kb(tmp_path / "kb.sqlite")
    monkeypatch.setattr(kb_store, "local_path", lambda proj: path)
    p.kb_file = path
    return p


@pytest.fixture()
def calls(monkeypatch, settings):
    got: list = []
    kb_fakes.install(monkeypatch, settings, got)
    return got


def _rebuild(project, items):
    project.kb_file.unlink()
    kb_fakes.build_kb(project.kb_file, items)


def test_sync_embeds_every_passage_with_its_header_once(project, calls):
    out = kb_vectors.sync(project)

    assert (out["passages"], out["embedded"], out["failed"]) == (3, 3, 0)
    row = KbPassage.objects.get(project=project, item_id="doc:ocv")
    assert (row.item_type, row.date, row.url, row.embedding_model) == ("gdoc", "2025-01-02", "https://gw/ocv",
                                                                       kb_fakes.MODEL)
    assert len(row.embedding) == 3072
    assert calls[0] == ("document", "Title: Oral cholera vaccines\nType: gdoc\nDate: 2025-01-02\n\n"
                                    "OCV stockpiles are limited.")
    calls.clear()
    again = kb_vectors.sync(project)
    assert (again["embedded"], again["kept"]) == (0, 3) and calls == []


def test_a_republish_embeds_only_what_changed_and_drops_what_went(project, calls):
    kb_vectors.sync(project)
    calls.clear()
    items = [dict(kb_fakes.ITEMS[0], text="OCV stockpiles are scarce."), kb_fakes.ITEMS[1],
             {"id": "page:new", "type": "page", "title": "New", "url": "https://gw/new", "text": "Fresh.",
              "sha256": "9"}]
    _rebuild(project, items)

    out = kb_vectors.sync(project)

    assert (out["embedded"], out["kept"], out["removed"]) == (2, 1, 1)
    assert sorted(t.split("\n")[0] for _, t in calls) == ["Title: New", "Title: Oral cholera vaccines"]
    assert set(KbPassage.objects.values_list("item_id", flat=True)) == {"doc:ocv", "blog:wash", "page:new"}


def test_vectors_from_another_model_count_as_missing(project, calls, settings):
    kb_vectors.sync(project)
    KbPassage.objects.update(embedding_model="vertex/older")
    assert kb_vectors.sync(project)["embedded"] == 3


def test_a_change_too_big_for_the_background_sync_waits_for_kb_embed(project, calls):
    out = kb_vectors.sync(project, max_embed=2)
    assert (out["deferred"], out["embedded"]) == (3, 0) and calls == []
    assert not KbPassage.objects.exists()


def test_a_passage_that_keeps_failing_is_left_for_the_next_run(project, monkeypatch, settings):
    kb_fakes.install(monkeypatch, settings, fail=lambda text: "Lead" in text)
    out = kb_vectors.sync(project)
    assert (out["embedded"], out["failed"]) == (2, 1)
    kb_fakes.install(monkeypatch, settings)
    assert kb_vectors.sync(project)["embedded"] == 1


def test_a_batch_that_fails_entirely_stops_the_sync(project, monkeypatch, settings):
    kb_fakes.install(monkeypatch, settings, fail=lambda text: True)
    with pytest.raises(RuntimeError, match="every passage"):
        kb_vectors.sync(project)


def test_nothing_is_sent_to_a_provider_that_keeps_what_it_is_sent(project, calls, monkeypatch):
    monkeypatch.setattr("apps.contacts.embeddings._check_retention", lambda m: (False, "caching is on"))
    with pytest.raises(RuntimeError, match="caching is on"):
        kb_vectors.sync(project)
    assert calls == []


def test_only_a_vertex_model_turns_kb_vectors_on(settings):
    settings.KB_EMBEDDINGS_MODEL = "cohere.embed-v4:0"
    assert kb_vectors.model() == ""
    settings.KB_EMBEDDINGS_MODEL = "vertex/gemini-embedding-001"
    assert kb_vectors.model() == "vertex/gemini-embedding-001"


def test_off_by_default_nothing_is_embedded_and_no_sync_starts(project, settings, monkeypatch,
                                                               django_capture_on_commit_callbacks):
    from apps.mcp.projects import _call

    got: list = []
    kb_fakes.install(monkeypatch, settings, got)
    settings.KB_VECTORS = False
    monkeypatch.setattr(connection, "vendor", "postgresql")  # even where pgvector could serve them
    monkeypatch.setattr("apps.mcp.projects.readable_project", lambda user, ws, h: project)
    started = []
    monkeypatch.setattr(kb_vectors, "_start", started.append)

    assert not kb_vectors.available() and kb_vectors.semantic_for(project) is None
    with django_capture_on_commit_callbacks(execute=True):
        assert kb_vectors.sync_after_publish(project) is False
    with pytest.raises(CommandError, match="KB_VECTORS"):
        call_command("kb_embed", "connect/givewell-kb")
    assert _call("connect", "givewell-kb", "search", {"query": "cholera"}).startswith("[1] doc:ocv")
    assert started == [] and got == [] and not KbPassage.objects.exists()


@sqlite_only
def test_on_sqlite_search_embeds_nothing_even_when_on(project, calls):
    from apps.mcp.projects import _call

    kb_vectors.sync(project)
    calls.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("apps.mcp.projects.readable_project", lambda user, ws, h: project)
        paraphrase = _call("connect", "givewell-kb", "search", {"query": "inoculation shortage"})
        lexical = _call("connect", "givewell-kb", "search", {"query": "cholera"})

    assert kb_vectors.semantic_for(project) is None
    assert paraphrase.startswith("No matches") and lexical.startswith("[1] doc:ocv")
    assert calls == []  # not even the question is embedded


def test_a_publish_starts_a_sync_only_where_vectors_are_searchable(project, monkeypatch, django_capture_on_commit_callbacks):
    started = []
    monkeypatch.setattr(kb_vectors, "_start", started.append)
    with django_capture_on_commit_callbacks(execute=True):
        assert kb_vectors.sync_after_publish(project) is False
    monkeypatch.setattr(kb_vectors, "available", lambda: True)
    with django_capture_on_commit_callbacks(execute=True):
        assert kb_vectors.sync_after_publish(project) is True
    assert started == [project.pk]


def test_the_background_sync_is_capped(project, calls, settings, monkeypatch):
    settings.KB_SYNC_MAX_EMBED = 1
    # its own thread closes its connection when done; here that is the test's
    monkeypatch.setattr(kb_vectors, "close_old_connections", lambda: None)
    kb_vectors._background_sync(project.pk)
    assert not KbPassage.objects.exists() and calls == []


@sqlite_only
def test_kb_embed_says_why_it_cannot_run_here(project):
    with pytest.raises(CommandError, match="PostgreSQL"):
        call_command("kb_embed", "connect/givewell-kb")
    with pytest.raises(CommandError, match="no workspace project"):
        call_command("kb_embed", "connect/nope")


def test_the_mcp_search_reranks_when_a_reranker_is_set(project, calls, settings, monkeypatch):
    from apps.mcp.projects import _call

    settings.KB_RERANK_MODEL = "semantic-ranker-default@latest"
    seen = []
    monkeypatch.setattr(kb_rerank, "reranker", lambda: lambda q, docs: seen.append(docs) or list(reversed(range(len(docs)))))
    monkeypatch.setattr("apps.mcp.projects.readable_project", lambda user, ws, h: project)
    hits = _call("connect", "givewell-kb", "search", {"query": "lead chlorine"})
    assert seen and hits.startswith("[1] blog:wash") and "[2] page:lead" in hits
