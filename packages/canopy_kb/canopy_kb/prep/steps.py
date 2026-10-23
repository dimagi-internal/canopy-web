"""The toolkit's steps as `canopy_kb.curate` steps: each result keyed by its inputs and
the code that makes it, stored on the host, computed only where the host lacks it.

| step | result | inputs |
|---|---|---|
| `reader(kind, …)` | a file's extracted text (`extract.extract_bytes`) | the file's sha256 |
| `card_step(publisher)` | an item's catalog card | its text's sha256, title, type, url, date, referrers' titles |
| `alias_step(publisher, …)` | the entity alias groups | the prompts (the most covered names and counts) |

A reader's REVISION is per format, so a PDF reader fix re-reads PDFs only. Every step
lists its source files and the libraries it reads with; `steps.lock` (beside this file)
records them, and `canopy_kb.curate.check_lock` refuses to run code it does not describe.
After changing one: bump its REVISION here if its output can change, then
`python -m canopy_kb.prep.steps --relock`.
"""
from __future__ import annotations

import concurrent.futures as cf
import sys
from pathlib import Path

from canopy_kb.curate import Budget, Curation, Step, check_lock, write_lock
from canopy_kb.prep import aliases as _aliases
from canopy_kb.prep import cards as _cards
from canopy_kb.prep import gemini
from canopy_kb.prep.raw import log

HERE = Path(__file__).parent
LOCK = HERE / "steps.lock"

#: Per format: (REVISION, the modules that read it, the libraries they read with).
READERS = {
    "page": ("2", ("extract.py", "html.py"), ("beautifulsoup4", "lxml", "markdownify")),
    "gdoc": ("2", ("extract.py", "html.py"), ("beautifulsoup4", "lxml", "markdownify")),
    "doc": ("2", ("extract.py", "html.py"), ("beautifulsoup4", "lxml", "markdownify")),
    "pdf": ("2", ("extract.py", "pdf.py"), ("pymupdf",)),
    "xlsx": ("2", ("extract.py", "xlsx.py"), ("openpyxl",)),
    "xls": ("2", ("extract.py", "xlsx.py"), ("openpyxl", "xlrd")),
    "docx": ("2", ("extract.py",), ("python-docx",)),
}
#: Estimated USD per call, for planning (Gemini 2.5 on Vertex, 2026-10).
CARD_COST, ALIAS_COST = 0.001, 0.75


def _src(*names: str) -> tuple[str, ...]:
    return tuple(str(HERE / n) for n in names)


def reader(kind: str, page_selector: str | None = None, title_suffix: str = "") -> Step:
    rev, mods, libs = READERS[kind]
    settings = {"kind": kind}
    if kind == "page":  # a page's reader takes the publisher's content region and title suffix
        settings |= {"page_selector": page_selector, "title_suffix": title_suffix}
    return Step(f"extract:{kind}", rev, settings, _src(*mods), libs)


def card_step(publisher: str, kinds: str = _cards.KINDS) -> Step:
    return Step("card", "1", {"prompt": _cards.PROMPT, "kinds": kinds, "publisher": publisher, "model": gemini.FLASH,
                              "max_tokens": _cards.MAX_TOKENS, "head": _cards.HEAD, "tail": _cards.TAIL},
                _src("cards.py", "gemini.py"), (), CARD_COST)


def alias_step(publisher: str, renames: str = "", apart: str = "") -> Step:
    return Step("aliases", "1", {"prompt": _aliases.PROMPT, "model": gemini.PRO, "top": _aliases.TOP,
                                 "publisher": publisher, "renames": renames, "apart": apart},
                _src("aliases.py", "gemini.py"), (), ALIAS_COST)


def all_steps() -> list[Step]:
    """The toolkit's steps as the lock knows them (settings aside: they are in the keys)."""
    return [reader(k) for k in READERS] + [card_step(""), alias_step("")]


def check() -> None:
    check_lock(all_steps(), LOCK)


# ---- running them -----------------------------------------------------------------------

def extract_key(step: Step, row: dict) -> str:
    return step.key(sha256=row["sha256"])


def extract(cur: Curation, fetch, jobs: list[tuple[dict, Step]], workers: int = 8) -> dict[str, dict]:
    """{key: result} for each (fetch row, reader step): stored results as they are, the
    rest read from the bytes (fetched this run, else the originals store) and stored. A
    format this machine cannot read gives no result."""
    from canopy_kb.prep.extract import extract_bytes

    keys = {extract_key(s, r): (r, s) for r, s in jobs}
    out = cur.results(keys)
    todo = [(k, r, s) for k, (r, s) in keys.items() if k not in out]
    if not todo:
        return out
    log(f"extract: {len(todo)} files to read ({len(out)} already read)")
    fetch.prefetch([r for _, r, _ in todo])
    with cf.ProcessPoolExecutor(workers) as ex:
        futs = {ex.submit(extract_bytes, s.settings["kind"], fetch.body(r), s.settings.get("page_selector"),
                          s.settings.get("title_suffix") or ""): k for k, r, s in todo}
        for n, f in enumerate(cf.as_completed(futs), 1):
            k = futs[f]
            res = f.result()
            if res.get("unavailable"):
                continue
            out[k] = res
            cur.put(k, res)
            if n % 500 == 0:
                log(f"  read {n}/{len(todo)}")
    fetch.forget_bodies()
    return out


def card_key(step: Step, it: dict, linked: list[str]) -> str:
    """An item's card is the model's reading of what its prompt shows: its text (by sha),
    title, type, url, date and its referrers' titles."""
    return step.key(text=it["sha256"], title=it["title"], type=it["type"], url=it["url"], date=it.get("date", ""),
                    linked=linked)


def cards(cur: Curation, step: Step, todo: list[tuple[str, dict, list[str]]], workers: int = 32) -> dict[str, dict]:
    """{key: card} for (key, item with its text, referrers' titles); each stored as it returns."""
    out: dict[str, dict] = {}
    publisher, kinds = step.settings["publisher"], step.settings["kinds"]

    def job(t):
        k, it, linked = t
        try:
            card = _cards.write(it, linked, publisher, kinds)
        except Exception as e:  # noqa: BLE001 - one failed card; the next refresh tries again
            log("  card failed:", it["id"], str(e)[:100])
            return
        cur.put(k, card, now=True)
        out[k] = card

    with cf.ThreadPoolExecutor(workers) as ex:
        list(ex.map(job, todo))
    return out


def aliases(cur: Curation, step: Step, all_cards: list[dict], budget: Budget | None = None) -> tuple[str, dict]:
    """(key, groups): the stored groups for these names, else the model's."""
    p = _aliases.prompts(all_cards, step.settings["publisher"], step.settings["renames"], step.settings["apart"])
    key = step.key(prompts=[p[k][1] for k in sorted(p)])
    have = cur.result(key)
    if have is not None:
        return key, have
    groups = _aliases.ask(p)
    cur.put(key, groups, now=True)
    return key, groups


if __name__ == "__main__":
    if "--relock" in sys.argv:
        print(write_lock(all_steps(), LOCK))
    else:
        check()
        print("steps.lock describes the code")
