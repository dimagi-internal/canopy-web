"""What an agent is told about a person, and in what order — hcp.rank / hcp.search.

* everything a grant reaches is ranked together, so the best entry wins wherever
  it lives (not the agent's own workspace first);
* what an agent learns on a board project is filed against it, and a later turn on
  that project finds it first;
* unmatched role entries are background, at most `ROLE_FLOOR` of them;
* with embeddings on (`HCP_EMBEDDINGS_MODEL`), entries are found by meaning too —
  vectors are filled lazily, cleared when a version stops being served, and any
  failure falls back to lexical.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.contacts import embeddings, hcp, people
from apps.contacts import services as contacts
from apps.contacts.models import PersonFact
from apps.harness import initiator as who
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

BASE = "/api/hcp"


@pytest.fixture(autouse=True)
def _granted(agents_granted):
    yield


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    org = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner, parent=org)
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    for space in (org, ws):
        WorkspaceMembership.objects.create(user=owner, workspace=space, role=WorkspaceMembership.OWNER)
        WorkspaceMembership.objects.create(user=lili, workspace=space, role=WorkspaceMembership.EDITOR)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner)
    login = User.objects.create_user("ace-login", "ace@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=login, workspace=ws, role=WorkspaceMembership.EDITOR)
    ace.user = login
    ace.save(update_fields=["user"])
    project = AgentProject.objects.create(agent=ace, ext_id="P1", name="Kenya rollout")
    task = AgentTask.objects.create(agent=ace, ext_id="T1", title="draft the plan", origin="manual",
                                    project=project, idempotency_key="t1")
    return {"org": org, "ws": ws, "lili": lili, "ace": ace, "project": project, "task": task,
            "person": contacts.person_for(user=lili)}


def _turn(w, key, *, task=None, prompt="hi"):
    turn, _ = services.enqueue_turn(
        agent=w["ace"], origin=Turn.ORIGIN_API, idempotency_key=key, prompt=prompt,
        initiator=who.for_user(w["lili"], via="chat", assurance=who.SESSION))
    if task is not None:
        turn.raised_from_task = task
        turn.save(update_fields=["raised_from_task"])
    return turn


def _add(w, statement, *, ws=None, kind="instance", project=None, category="work_context"):
    return people.record_fact(person=w["person"], workspace=ws or w["ws"], kind=kind,
                              statement=statement, category=category, project=project)


def _search(w, turn, query, **body):
    c = Client()
    c.force_login(w["ace"].user)
    body = {"query": query, "categories": ["work_context"], "purpose": "answer", **body}
    r = c.post(f"{BASE}/v1/preferences/search?turn={turn.pk}", data=json.dumps(body),
               content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()


def _said(body):
    return [e["claim"]["subject"]["preference"] for e in body["entries"]]


def test_the_best_entry_wins_wherever_it_lives(w):
    for n in range(6):
        _add(w, f"Reviews kangaroo notes {n}.")                    # own workspace, one word
    _add(w, "Leads kangaroo care coach training.", ws=w["org"])    # the organisation, three words
    body = _search(w, _turn(w, "a"), "kangaroo care coach", maxEntries=1)
    assert _said(body) == ["Leads kangaroo care coach training."]


def test_what_an_agent_learns_on_a_project_is_filed_against_it(w):
    c = Client()
    c.force_login(w["ace"].user)
    turn = _turn(w, "b", task=w["task"])
    r = c.post(f"{BASE}/v1/preferences/add?turn={turn.pk}", content_type="application/json",
               data=json.dumps({"category": "work_context", "preference": "Wants the plan by Friday.",
                                "declarationType": "user-declared", "sourceContext": f"turn:{turn.pk}"}))
    assert r.status_code == 201, r.content
    assert PersonFact.objects.get(statement="Wants the plan by Friday.").project == w["project"]
    # Not dispatched from a task: no project.
    plain = _turn(w, "c")
    c.post(f"{BASE}/v1/preferences/add?turn={plain.pk}", content_type="application/json",
           data=json.dumps({"category": "work_context", "preference": "Likes tables.",
                            "declarationType": "user-declared", "sourceContext": f"turn:{plain.pk}"}))
    assert PersonFact.objects.get(statement="Likes tables.").project is None


def test_a_turn_on_a_project_finds_that_projects_entries_first(w):
    _add(w, "Wants the plan by Friday.", project=w["project"])
    _add(w, "Owns the finance dashboard.")
    on_project = _search(w, _turn(w, "d", task=w["task"]), "what should I write")
    assert _said(on_project) == ["Wants the plan by Friday."]
    elsewhere = _search(w, _turn(w, "e"), "what should I write")
    assert _said(elsewhere) == []                                   # never padded


def test_unmatched_role_entries_come_along_but_only_a_few(w):
    for n in range(5):
        _add(w, f"Role note {n}.", kind="role")
    _add(w, "Call me JJ, not Jonathan.", kind="correction", category="general_preferences")
    body = _search(w, _turn(w, "f"), "unrelated question",
                   categories=["work_context", "general_preferences"], maxEntries=20)
    said = _said(body)
    assert said[0] == "Call me JJ, not Jonathan."                   # corrections always, first
    assert len(said) == 1 + hcp.ROLE_FLOOR


# --- embeddings ------------------------------------------------------------------

#: A toy embedding model: one axis per idea, so "concise" and "long" land together.
_IDEAS = [{"concise", "brief", "short", "long", "length"}, {"finance", "budget", "money"}]


def _fake_invoke(calls):
    def invoke(model_id, texts, purpose):
        calls.append((purpose, len(texts)))
        out = []
        for t in texts:
            words = set(t.lower().replace(".", " ").replace("?", " ").split())
            out.append([1.0 if words & idea else 0.0 for idea in _IDEAS] + [0.1])
        return out
    return invoke


@pytest.fixture(autouse=True)
def _fresh_retention_check(monkeypatch):
    monkeypatch.setitem(embeddings._retention, "model", None)


@pytest.fixture()
def embeddings_on(settings, monkeypatch):
    settings.HCP_EMBEDDINGS_MODEL = "cohere.embed-multilingual-v3"
    calls: list = []
    monkeypatch.setattr(embeddings, "_invoke", _fake_invoke(calls))
    monkeypatch.setattr(embeddings, "_check_retention", lambda m: (True, "keeps nothing"))
    return calls


def test_with_embeddings_on_an_entry_is_found_by_meaning(w, embeddings_on):
    _add(w, "Prefers concise replies.")
    _add(w, "Owns the quarterly dashboard.")
    body = _search(w, _turn(w, "g"), "how long should a status message be")
    assert _said(body) == ["Prefers concise replies."]              # no word in common
    assert body["minimization"]["method"] == "hybrid"
    fact = PersonFact.objects.get(statement="Prefers concise replies.")
    assert fact.embedding and fact.embedding_model == "cohere.embed-multilingual-v3"
    assert ("query", 1) in embeddings_on and ("document", 2) in embeddings_on
    # Filled once: the next search embeds only the query.
    embeddings_on.clear()
    _search(w, _turn(w, "h"), "budget")
    assert embeddings_on == [("query", 1)]


def test_the_similarity_every_entry_gets_does_not_admit_it(w, embeddings_on, monkeypatch):
    # gemini-embedding-001 scores an unrelated question ~0.5 against anything; at that
    # floor meaning alone would hand over the whole memory.
    _add(w, "Owns the quarterly dashboard.")
    monkeypatch.setattr(embeddings, "similarity", lambda a, b: 0.55)
    assert _said(_search(w, _turn(w, "g2"), "recipe for banana bread")) == []
    monkeypatch.setattr(embeddings, "similarity", lambda a, b: 0.7)
    assert _said(_search(w, _turn(w, "g3"), "recipe for banana bread")) == ["Owns the quarterly dashboard."]


def test_a_vector_is_cleared_when_its_version_stops_being_served(w, embeddings_on,
                                                                 django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        fact = _add(w, "Prefers concise replies.")
    fact.refresh_from_db()
    assert fact.embedding                                           # embedded on write
    with django_capture_on_commit_callbacks(execute=True):
        new = people.record_fact(person=w["person"], workspace=w["ws"], kind="instance",
                                 statement="Prefers short replies.", supersedes=fact)
    fact.refresh_from_db()
    assert fact.embedding is None and fact.embedding_model == ""
    people.retract(new, by=w["lili"])
    new.refresh_from_db()
    assert new.embedding is None


@pytest.mark.parametrize("check", [
    lambda m: (False, "caching is on"),
    lambda m: (_ for _ in ()).throw(RuntimeError("403 from cacheConfig")),
])
def test_nothing_is_sent_until_the_provider_is_verified_to_keep_nothing(w, embeddings_on, monkeypatch, check):
    monkeypatch.setattr(embeddings, "_check_retention", check)
    _add(w, "Prefers concise replies.")
    assert _said(_search(w, _turn(w, "k"), "how long should a status message be")) == []
    assert embeddings_on == []                                      # no text left canopy
    with pytest.raises(Exception, match="not sending memory text"):
        call_command("hcp_embed")


def test_a_passed_retention_check_is_trusted_for_a_while_not_forever(w, embeddings_on, monkeypatch):
    checks = []
    monkeypatch.setattr(embeddings, "_check_retention", lambda m: checks.append(m) or (True, "ok"))
    embeddings.embed(["a"])
    embeddings.embed(["b"])
    assert len(checks) == 1
    monkeypatch.setitem(embeddings._retention, "at", embeddings._retention["at"] - embeddings.RETENTION_TTL - 1)
    embeddings.embed(["c"])
    assert len(checks) == 2


def test_vertex_asks_for_retrieval_embeddings_one_text_at_a_time(settings, monkeypatch):
    settings.HCP_EMBEDDINGS_MODEL = "vertex/gemini-embedding-001"
    settings.HCP_VERTEX_PROJECT, settings.HCP_VERTEX_LOCATION = "canopy-494811", "us-central1"
    posts = []

    class Session:
        def post(self, url, json, timeout):
            posts.append((url, json))

            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"predictions": [{"embeddings": {"values": [1.0, 0.0]}} for _ in json["instances"]]}
            return R()
    monkeypatch.setattr(embeddings, "_vertex_session", lambda: Session())
    monkeypatch.setattr(embeddings, "_check_retention", lambda m: (True, "caching disabled"))
    assert len(embeddings.embed(["one", "two"], purpose="query")) == 2
    assert [u for u, _ in posts] == [
        "https://us-central1-aiplatform.googleapis.com/v1/projects/canopy-494811/locations/us-central1"
        "/publishers/google/models/gemini-embedding-001:predict"] * 2
    assert posts[0][1]["instances"] == [{"content": "one", "task_type": "RETRIEVAL_QUERY"}]


def test_vertex_retention_means_the_projects_cache_is_disabled(settings, monkeypatch):
    settings.HCP_EMBEDDINGS_MODEL = "vertex/gemini-embedding-001"
    settings.HCP_VERTEX_PROJECT = "canopy-494811"

    def session(body):
        class S:
            def get(self, url, timeout):
                assert url.endswith("/projects/canopy-494811/cacheConfig")

                class R:
                    def raise_for_status(self):
                        pass

                    def json(self):
                        return body
                return R()
        return lambda: S()
    monkeypatch.setattr(embeddings, "_vertex_session", session({"disableCache": True}))
    assert embeddings.retention_ok(force=True)[0] is True
    monkeypatch.setattr(embeddings, "_vertex_session", session({"retentionType": "DURABLE"}))
    assert embeddings.retention_ok(force=True)[0] is False


def test_a_failing_model_falls_back_to_lexical(w, settings, monkeypatch):
    settings.HCP_EMBEDDINGS_MODEL = "cohere.embed-multilingual-v3"

    def down(*a, **k):
        raise RuntimeError("bedrock is down")
    monkeypatch.setattr(embeddings, "_invoke", down)
    monkeypatch.setattr(embeddings, "_check_retention", lambda m: (True, "keeps nothing"))
    _add(w, "Owns the finance dashboard.")
    assert _said(_search(w, _turn(w, "i"), "finance dashboard")) == ["Owns the finance dashboard."]


def test_vectors_from_another_model_count_as_none(w, embeddings_on):
    fact = _add(w, "Prefers concise replies.")
    PersonFact.objects.filter(pk=fact.pk).update(embedding=b"\x00" * 12, embedding_model="old-model")
    fact.refresh_from_db()
    assert not embeddings.has_current(fact)
    call_command("hcp_embed")
    fact.refresh_from_db()
    assert fact.embedding_model == "cohere.embed-multilingual-v3"


def test_with_embeddings_off_nothing_is_sent_anywhere(w, monkeypatch):
    def never(*a, **k):
        raise AssertionError("called the model with embeddings off")
    monkeypatch.setattr(embeddings, "_invoke", never)
    _add(w, "Owns the finance dashboard.")
    body = _search(w, _turn(w, "j"), "finance")
    assert body["minimization"]["method"] == "lexical"
    assert PersonFact.objects.get().embedding is None
