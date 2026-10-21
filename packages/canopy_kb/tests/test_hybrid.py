"""Hybrid search: the embedding input, fusion with a host's vectors, the lexical
fallback, and the eval metrics — plain pytest, no vectors store (a fake `semantic`)."""
import json

import pytest

from canopy_kb import KB, KBError, build
from canopy_kb.evaluate import score
from canopy_kb.hybrid import TEXT_MAX, embedding_text, passage_sha, rrf
from canopy_kb.tools import run, tool_specs

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


def test_rrf_rewards_agreement_and_breaks_ties_by_first_list():
    assert rrf([["a", "b"], ["b", "x"]]) == ["b", "a", "x"]
    assert rrf([["a"], ["b"]]) == ["a", "b"]


def _semantic(pairs, calls=None):
    def semantic(query, *, types=None, since="", until="", limit=50):
        if calls is not None:
            calls.append({"query": query, "types": types, "since": since, "limit": limit})
        return pairs
    return semantic


def test_without_vectors_every_mode_is_lexical(kb):
    lexical = kb.search("cholera", mode="lexical")
    assert kb.search("cholera") == lexical
    assert kb.search("cholera", mode="semantic") == lexical
    assert kb.search("cholera", mode="hybrid", semantic=_semantic(None)) == lexical


def test_semantic_finds_what_shares_no_words_with_the_question(kb):
    question = "how many doses can be made"
    assert kb.search(question, mode="lexical") == []
    hits = kb.search(question, mode="semantic", semantic=_semantic([("doc:ocv", 0), ("doc:ocv", 0)]))
    assert [h["id"] for h in hits] == ["doc:ocv"]  # one row per item
    assert hits[0]["url"] == "https://x/ocv" and hits[0]["snippet"].startswith("Global stockpiles")


def test_hybrid_fuses_both_lists_one_row_per_item(kb):
    calls = []
    hits = kb.search("cholera", limit=5, semantic=_semantic([("page:lead", 0), ("blog:wash", 0)], calls))
    assert {h["id"] for h in hits} == {"doc:ocv", "blog:wash", "page:lead"}
    assert hits[0]["id"] == "blog:wash"  # ranked by both
    assert "«" in hits[0]["snippet"]  # the lexical row, with the matched words marked
    assert calls[0]["limit"] >= 50


def test_a_vector_for_a_passage_the_file_no_longer_has_is_skipped(kb):
    assert kb.search("x y", mode="semantic", semantic=_semantic([("gone:1", 0), ("page:lead", 0)]))[0]["id"] == "page:lead"


def test_filters_reach_the_semantic_side(kb):
    calls = []
    kb.search("water", types=["blog_post"], since="2026-01-01", semantic=_semantic([], calls))
    assert calls[0]["types"] == ["blog_post"] and calls[0]["since"] == "2026-01-01"


def test_an_unknown_mode_is_refused(kb):
    with pytest.raises(KBError):
        kb.search("cholera", mode="vibes")


def test_the_search_tool_pools_the_hosts_nearest_passages(kb):
    spec = next(t for t in tool_specs(kb) if t["name"] == "search")
    assert {"query", "types", "entity", "since", "until", "k"} == set(spec["inputSchema"]["properties"])
    assert run(kb, "search", {"query": "doses made"}).startswith("No matches")
    out = run(kb, "search", {"query": "doses made"}, semantic=_semantic([("doc:ocv", 0)]))
    assert out.startswith("[1] doc:ocv · gdoc") and "Global stockpiles" in out


def test_the_search_tool_lets_the_reranker_order_the_whole_pool(kb):
    seen = []

    def rerank(query, docs):
        seen.extend(docs)
        return sorted(range(len(docs)), key=lambda i: "WASH" not in docs[i]["title"])

    out = run(kb, "search", {"query": "cholera"}, semantic=_semantic([("page:lead", 0)]), rerank=rerank)
    assert out.startswith("[1] blog:wash")
    assert {d["title"] for d in seen} == {"Oral cholera vaccines", "WASHmobile", "Lead exposure"}
    assert run(kb, "search", {"query": "cholera"}, rerank=lambda q, d: [9]).startswith("[1] ")


def test_score_reports_recall_at_k_and_mrr_by_kind():
    golden = [{"q": "a", "kind": "keyword", "rel": ["x"]}, {"q": "b", "kind": "paraphrase", "rel": ["y", "z"]},
              {"q": "c", "kind": "paraphrase", "rel": ["nope"]}]
    ranked = {"a": ["x"], "b": ["1", "2", "3", "4", "5", "6", "z"], "c": ["x"]}
    res = score(golden, lambda q: ranked[q])
    assert res["by_kind"]["keyword"]["recall@5"] == 1.0
    assert res["by_kind"]["paraphrase"]["hits@5"] == 0 and res["by_kind"]["paraphrase"]["hits@10"] == 1
    assert res["overall"]["mrr"] == round((1 + 1 / 7) / 3, 3)


def test_weights_tilt_the_fusion_toward_one_list():
    assert rrf([["a", "b"], ["b", "a"]], weights=[0.7, 0.3])[0] == "a"
    assert rrf([["a", "b"], ["b", "a"]], weights=[0.3, 0.7])[0] == "b"


def test_a_reranker_reorders_the_fused_candidates_seeing_each_passage(kb):
    seen = []

    def rerank(query, docs):
        seen.extend(docs)
        return sorted(range(len(docs)), key=lambda i: "Lead" not in docs[i]["title"])

    hits = kb.search("cholera lead", semantic=_semantic([("doc:ocv", 0), ("page:lead", 0)]), rerank=rerank)
    assert hits[0]["id"] == "page:lead"
    assert {"title": "Lead exposure", "text": "Lead paint and spices."} in seen


@pytest.mark.parametrize("answer", [None, [], [0, 0], [7]])
def test_a_reranker_that_cannot_answer_leaves_the_order(kb, answer):
    plain = kb.search("cholera", semantic=_semantic([("page:lead", 0)]))
    assert kb.search("cholera", semantic=_semantic([("page:lead", 0)]), rerank=lambda q, d: answer) == plain


def test_a_partial_rerank_keeps_the_rest_in_order(kb):
    hits = kb.search("cholera", semantic=_semantic([("page:lead", 0), ("blog:wash", 0)]), rerank=lambda q, d: [2])
    assert len(hits) == 3 and hits[0]["id"] == kb.search("cholera", semantic=_semantic(
        [("page:lead", 0), ("blog:wash", 0)]))[2]["id"]
