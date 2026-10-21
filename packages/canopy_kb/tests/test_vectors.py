"""Search's host hooks — the reranker (live) and the dormant vector hooks: what a passage's
vector would be made of, and a host's nearest passages joining the pool. Plain pytest, a
fake `semantic` and `rerank`."""
import json

import pytest

from canopy_kb import KB, build
from canopy_kb.tools import run, tool_specs
from canopy_kb.vectors import TEXT_MAX, embedding_text, passage_sha

ITEMS = [
    {"id": "doc:ocv", "type": "gdoc", "title": "Oral cholera vaccines", "url": "https://x/ocv",
     "date": "2025-01-02", "text": "Global stockpiles of OCV are limited by manufacturing capacity.", "sha256": "1"},
    {"id": "blog:wash", "type": "blog_post", "title": "WASHmobile", "url": "https://x/wash",
     "date": "2026-07-23", "text": "Chlorine and soap delivered to households after a cholera case.", "sha256": "2"},
    {"id": "page:lead", "type": "page", "title": "Lead exposure", "url": "https://x/lead",
     "date": "", "text": "Lead paint and spices.", "sha256": "3"},
]


@pytest.fixture()
def kb(tmp_path):
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "manifest.json").write_text(json.dumps({"name": "t", "publisher": "T"}))
    (b / "items.jsonl").write_text("\n".join(json.dumps(i) for i in ITEMS))
    build(b, tmp_path / "kb.sqlite")
    return KB(tmp_path / "kb.sqlite")


def _semantic(pairs, calls=None):
    def semantic(query, *, types=None, since="", until="", limit=50):
        if calls is not None:
            calls.append({"query": query, "types": types, "since": since, "limit": limit})
        return pairs
    return semantic


def test_the_search_tool_lets_the_reranker_order_the_whole_pool(kb):
    seen = []

    def rerank(query, docs):
        seen.extend(docs)
        return sorted(range(len(docs)), key=lambda i: "WASH" not in docs[i]["title"])

    out = run(kb, "search", {"query": "cholera lead"}, rerank=rerank)
    assert out.startswith("[1] blog:wash")
    assert {d["title"] for d in seen} == {"Oral cholera vaccines", "WASHmobile", "Lead exposure"}


@pytest.mark.parametrize("answer", [None, [], [0, 0], [9]])
def test_a_reranker_that_cannot_answer_leaves_the_word_order(kb, answer):
    assert run(kb, "search", {"query": "cholera"}, rerank=lambda q, d: answer) == run(kb, "search", {"query": "cholera"})


def test_the_embedding_input_carries_a_header_on_every_passage():
    text = embedding_text("Oral cholera vaccines", "charity_review", "2025-01-02", "Second passage.")
    assert text == "Title: Oral cholera vaccines\nType: charity review\nDate: 2025-01-02\n\nSecond passage."
    assert embedding_text("", "", "", "just text") == "just text"
    assert len(embedding_text("t", "page", "", "x" * 5000)) == TEXT_MAX


def test_a_title_change_changes_the_key_a_vector_is_stored_under():
    assert passage_sha(embedding_text("A", "page", "", "b")) != passage_sha(embedding_text("B", "page", "", "b"))
    assert passage_sha(embedding_text("A", "page", "", "b")) == passage_sha(embedding_text("A", "page", "", "b"))


def test_passages_lists_every_passage_with_its_key(kb):
    got = list(kb.passages())
    assert [(p["item_id"], p["ord"]) for p in got] == [("doc:ocv", 0), ("blog:wash", 0), ("page:lead", 0)]
    assert got[0]["sha256"] == passage_sha(got[0]["text"]) and got[0]["text"].startswith("Title: Oral cholera")


def test_a_hosts_nearest_passages_join_the_pool_when_it_has_vectors(kb):
    spec = next(t for t in tool_specs(kb) if t["name"] == "search")
    assert {"query", "types", "entity", "since", "until", "k"} == set(spec["inputSchema"]["properties"])
    assert run(kb, "search", {"query": "doses made"}).startswith("No matches")
    calls = []
    out = run(kb, "search", {"query": "doses made", "types": ["gdoc"]}, semantic=_semantic([("doc:ocv", 0)], calls))
    assert out.startswith("[1] doc:ocv · gdoc") and "Global stockpiles" in out
    assert calls[0]["types"] == ["gdoc"]
    assert run(kb, "search", {"query": "doses made"}, semantic=_semantic(None)).startswith("No matches")
