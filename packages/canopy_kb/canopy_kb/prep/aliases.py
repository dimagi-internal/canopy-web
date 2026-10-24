"""Entity names that are one thing, which no string rule can see: renames, acronyms used
alone, synonyms ("malaria nets" = "insecticide-treated nets").

The most-covered names of each kind (from the cards) go to the AI session running the
refresh in one list per kind (`prompts`); it returns groups, kept by `groups`. When the
names and counts are what they were last time, the stored groups are kept and nothing is
asked again (`steps.aliases`). The engine merges
spelling variants and spelled-out acronyms itself; this is for the rest. A curator passes
examples from its own publisher (renames it knows), which steer the model better than
generic ones.
"""
from __future__ import annotations

import collections
import re


TOP = {"org": 1500, "program": 1200}

PROMPT = """Below are {kind} names extracted from {publisher}'s documents, with document counts.
Group names that refer to the SAME {what}: renames{examples}, acronyms, spelling and punctuation
variants, with/without "Initiative", "Foundation", "Inc". For programs, group only true synonyms
(e.g. "long-lasting insecticidal nets" = "insecticide-treated nets" = "malaria nets"; "deworming" =
"mass deworming"), NOT broader/narrower programs ("malaria prevention" is not "insecticide-treated
nets"; "cash transfers" is not "conditional cash transfers"). Do not group distinct organisations: a
parent and its program or affiliate stay apart{apart}, as do a ministry of health in different countries.

Return JSON: [{{"canonical": "<preferred current name>", "names": ["every name in the group, exactly as listed"]}}]
Only groups with 2+ names.

{names}"""


def _key(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", re.sub(r"\([^)]*\)", " ", name.lower())).split())


def prompts(cards, publisher: str, renames: str = "", apart: str = "") -> dict[str, tuple[list, str]]:
    """{kind: (rows of (name, count), the prompt)} from the cards' names, most covered first.
    The prompts are what the groups depend on: equal prompts, equal groups (`steps.aliases` keeps them)."""
    counts = {"org": collections.Counter(), "program": collections.Counter()}
    spelled: dict[str, dict[str, str]] = {"org": {}, "program": {}}
    for card in cards:
        card = card or {}
        for kind, field in (("org", "orgs"), ("program", "programs")):
            for n in card.get(field) or []:
                if isinstance(n, str) and n.strip() and len(n) <= 120:
                    k = _key(n)
                    counts[kind][k] += 1
                    spelled[kind].setdefault(k, n.strip())
    out = {}
    for kind, top in TOP.items():
        rows = [(spelled[kind][k], n) for k, n in counts[kind].most_common(top)]
        names = "\n".join(f"{name} ({n})" for name, n in rows)
        what = "organisation" if kind == "org" else "intervention/program"
        out[kind] = (rows, PROMPT.format(kind=kind, what=what, publisher=publisher, names=names,
                                         examples=f" ({renames})" if renames else "",
                                         apart=f" ({apart})" if apart else ""))
    return out


def groups(rows: list, raw) -> list[dict]:
    """The groups a writer (a model, a session) returned for one kind's prompt, kept to the
    names it was shown and to groups of two or more; ValueError if it is not a list."""
    if not isinstance(raw, list):
        raise ValueError("alias groups are a JSON list of {canonical, names}")
    valid = {name for name, _ in rows}
    out = [{"canonical": g["canonical"], "names": [x for x in g.get("names", []) if x in valid]}
           for g in raw if isinstance(g, dict) and g.get("canonical")]
    return [g for g in out if len(g["names"]) >= 2]
