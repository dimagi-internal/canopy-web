"""Entity names that are one thing, which no string rule can see: renames, acronyms used
alone, synonyms ("malaria nets" = "insecticide-treated nets").

The most-covered names of each kind (from the cards) go to Gemini 2.5 Pro in one list per
kind; it returns groups, written to <data>/aliases.json for the bundle. When the names and
counts are what they were last time (<data>/aliases.key), the model is not asked again. The engine merges
spelling variants and spelled-out acronyms itself; this is for the rest. A curator passes
examples from its own publisher (renames it knows), which steer the model better than
generic ones.
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
from pathlib import Path

from canopy_kb.prep import gemini
from canopy_kb.prep.raw import log, read_json, write_json

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
    The prompts are what the groups depend on: equal prompts, equal groups (`run` keeps them)."""
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


def ask(prompts_: dict[str, tuple[list, str]]) -> dict:
    """The model's groups for each kind's prompt (Gemini 2.5 Pro)."""
    out = {}
    for kind, (rows, prompt) in prompts_.items():
        groups, _ = gemini.generate_json(gemini.PRO, [{"text": prompt}], max_tokens=65000, thinking=None,
                                         timeout=900, tries=3)
        valid = {name for name, _ in rows}
        out[kind] = [{"canonical": g["canonical"], "names": [x for x in g.get("names", []) if x in valid]}
                     for g in groups if isinstance(g, dict) and g.get("canonical")]
        out[kind] = [g for g in out[kind] if len(g["names"]) >= 2]
        log(f"aliases: {kind}: {len(out[kind])} groups over the top {len(rows)} names")
    return out


def run(data: Path, publisher: str, renames: str = "", apart: str = "") -> dict:
    """`renames`: e.g. "SCI Foundation -> Unlimit Health"; `apart`: e.g. "Evidence Action and
    its Deworm the World Initiative are two names"."""
    cards = read_json(data / "cards.json", {})
    prompts_ = prompts([c.get("card") for c in cards.values()], publisher, renames, apart)
    # Same names and counts as last time: keep the groups. Asking again would regroup them a
    # little differently (the model is not deterministic), which is a new version of the KB
    # with nothing new in it.
    key = hashlib.sha256(json.dumps([prompts_[k][1] for k in sorted(prompts_)]).encode()).hexdigest()
    key_path = data / "aliases.key"
    if (data / "aliases.json").exists() and key_path.exists() and key_path.read_text().strip() == key:
        log("aliases: names unchanged since the last run; keeping its groups")
        return {k: len(v) for k, v in read_json(data / "aliases.json", {}).items()}
    out = ask(prompts_)
    write_json(data / "aliases.json", out)
    key_path.write_text(key)
    return {k: len(v) for k, v in out.items()}
