"""The KB search reranker: the Vertex AI ranking API call, its host, and that a failure
leaves the word order (an MCP search through it is in tests/test_kb_vectors.py)."""
from __future__ import annotations

from apps.agents import kb_rerank


class _RankSession:
    def __init__(self, records=None, fail=False):
        self.sent, self.records, self.fail = [], records, fail

    def post(self, url, json, timeout):
        self.sent.append((url, json))
        if self.fail:
            raise TimeoutError("ranking API timed out")
        records = self.records

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"records": records}
        return R()


def test_no_reranker_unless_one_is_configured(settings):
    settings.KB_RERANK_MODEL = ""
    assert kb_rerank.reranker() is None
    settings.KB_RERANK_MODEL = "semantic-ranker-default@latest"
    assert kb_rerank.reranker() is not None


def test_the_reranker_sends_each_passage_and_reads_back_the_order(settings, monkeypatch):
    settings.KB_RERANK_MODEL = "semantic-ranker-default@latest"
    session = _RankSession([{"id": "1", "score": 0.9}, {"id": "0", "score": 0.2}])
    monkeypatch.setattr("apps.contacts.embeddings._vertex_session", lambda: session)

    order = kb_rerank.reranker()("jab shortage", [{"title": "Lead", "text": "paint"}, {"title": "OCV", "text": "scarce"}])

    assert order == [1, 0]
    url, body = session.sent[0]
    assert url.startswith("https://us-discoveryengine.googleapis.com/v1/projects/")
    assert url.endswith("/locations/us/rankingConfigs/default_ranking_config:rank")
    assert body["model"] == "semantic-ranker-default@latest" and body["records"][1]["content"] == "scarce"


def test_the_reranker_location_picks_its_host(settings, monkeypatch):
    settings.KB_RERANK_MODEL = "semantic-ranker-default@latest"
    session = _RankSession([])
    monkeypatch.setattr("apps.contacts.embeddings._vertex_session", lambda: session)
    for loc, host in (("global", "discoveryengine"), ("eu", "eu-discoveryengine")):
        settings.KB_RERANK_LOCATION = loc
        kb_rerank.reranker()("q", [{"title": "a", "text": "b"}])
        url = session.sent[-1][0]
        assert url.startswith(f"https://{host}.googleapis.com/") and f"/locations/{loc}/" in url


def test_a_failed_rerank_answers_none(settings, monkeypatch):
    settings.KB_RERANK_MODEL = "semantic-ranker-default@latest"
    monkeypatch.setattr("apps.contacts.embeddings._vertex_session", lambda: _RankSession(fail=True))
    assert kb_rerank.reranker()("q", [{"title": "a", "text": "b"}]) is None
