"""Measure a KB's search against a golden set: does the right item come back, and how high?

A golden set is a JSON list of questions, each with the item ids that answer it:

    [{"q": "oral cholera vaccine", "kind": "keyword", "rel": ["doc:1Lrs…"]}, …]

`kind` is free text ("keyword", "paraphrase") and gets its own breakdown, because
the two fail differently: BM25 is strong on the words a document uses and blind
to a question that uses other words for the same thing.

Metrics, per mode:
  recall@k  share of questions with at least one relevant item in the top k
  mrr       mean of 1/rank of the first relevant item within the deepest k (0 if none)
"""
from __future__ import annotations

from typing import Callable, Sequence

KS = (5, 10, 50)


def first_hit(ranked: Sequence[str], relevant: set[str]) -> int | None:
    """1-based rank of the first relevant id, or None."""
    for rank, item_id in enumerate(ranked, 1):
        if item_id in relevant:
            return rank
    return None


def score(golden: list[dict], ranked_ids: Callable[[str], Sequence[str]], ks: Sequence[int] = KS) -> dict:
    """Run every question through `ranked_ids(question)` (ids, best first, at least
    max(ks) deep) and report recall@k and MRR, overall and per kind."""
    depth = max(ks)
    rows = []
    for g in golden:
        ranked = list(ranked_ids(g["q"]))[:depth]
        rows.append({"q": g["q"], "kind": g.get("kind", ""), "rank": first_hit(ranked, set(g["rel"]))})

    def summarise(subset: list[dict]) -> dict:
        n = len(subset)
        out: dict = {"n": n}
        for k in ks:
            hits = sum(1 for r in subset if r["rank"] is not None and r["rank"] <= k)
            out[f"recall@{k}"] = round(hits / n, 3) if n else 0.0
            out[f"hits@{k}"] = hits
        out["mrr"] = round(sum(1.0 / r["rank"] for r in subset if r["rank"]) / n, 3) if n else 0.0
        return out

    kinds = sorted({r["kind"] for r in rows})
    return {"overall": summarise(rows),
            "by_kind": {kind: summarise([r for r in rows if r["kind"] == kind]) for kind in kinds},
            "questions": rows}


def table(results: dict[str, dict], ks: Sequence[int] = KS) -> str:
    """A plain-text table of `{mode: score(...)}`, one block per kind then overall."""
    head = f"{'mode':<16} {'slice':<20} " + " ".join(f"{'R@' + str(k):>7}" for k in ks) + f" {'MRR':>6}"
    lines = [head, "-" * len(head)]
    for mode, res in results.items():
        for label, s in [*res["by_kind"].items(), ("overall", res["overall"])]:
            cells = " ".join(f"{s[f'hits@{k}']:>3}/{s['n']:<3}" for k in ks)
            lines.append(f"{mode:<16} {label:<20} {cells} {s['mrr']:>6.3f}")
    return "\n".join(lines)
