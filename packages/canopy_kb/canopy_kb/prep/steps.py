"""The toolkit's steps as `canopy_kb.curate` steps: each result keyed by its inputs and
the code that makes it, stored on the host, computed only where the host lacks it.

| step | result | inputs |
|---|---|---|
| `reader(kind, …)` | a file's extracted text (`extract.extract_bytes`) | the file's sha256 |
| `card_step(publisher)` | an item's catalog card | its text's sha256, title, type, url, date, referrers' titles |
| `alias_step(publisher, …)` | the entity alias groups | the prompts (the most covered names and counts) |

The model steps run on an engine (`engine.py`): the AI session running the refresh, or
Gemini on Vertex. Which model reads a prompt is the step's `meta`, not its key.

A reader's REVISION is per format, so a PDF reader fix re-reads PDFs only. Every step
lists its source files and the libraries it reads with; `steps.lock` (beside this file)
records them, and `canopy_kb.curate.check_lock` refuses to run code it does not describe.
After changing one: bump its REVISION here if its output can change, then
`python -m canopy_kb.prep.steps --relock`.
"""
from __future__ import annotations

import concurrent.futures as cf
import concurrent.futures.process  # noqa: F401 - BrokenProcessPool
import os
import sys
from pathlib import Path

from canopy_kb.curate import Budget, Curation, CurationError, Step, check_lock, write_lock
from canopy_kb.prep import aliases as _aliases
from canopy_kb.prep import cards as _cards
from canopy_kb.prep import engine as _engine
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


#: The model each step names was in its key until 2026-10-24; it is metadata now (`meta`),
#: and `formerly` keeps what was made under it found.
_FORMER_MODEL = {"card": "gemini-2.5-flash", "aliases": "gemini-2.5-pro"}


def card_step(publisher: str, kinds: str = _cards.KINDS) -> Step:
    settings = {"prompt": _cards.PROMPT, "kinds": kinds, "publisher": publisher,
                "max_tokens": _cards.MAX_TOKENS, "head": _cards.HEAD, "tail": _cards.TAIL}
    return Step("card", "1", settings, _src("cards.py", "gemini.py", "engine.py"), (), CARD_COST,
                meta={"model": gemini.FLASH, "call": {"max_tokens": _cards.MAX_TOKENS}},
                formerly=({**settings, "model": _FORMER_MODEL["card"]},))


def alias_step(publisher: str, renames: str = "", apart: str = "") -> Step:
    settings = {"prompt": _aliases.PROMPT, "top": _aliases.TOP, "publisher": publisher, "renames": renames,
                "apart": apart}
    return Step("aliases", "1", settings, _src("aliases.py", "gemini.py", "engine.py"), (), ALIAS_COST,
                meta={"model": gemini.PRO, "call": {"max_tokens": 65000, "thinking": None, "timeout": 900, "tries": 3}},
                formerly=({**settings, "model": _FORMER_MODEL["aliases"]},))


def all_steps() -> list[Step]:
    """The toolkit's steps as the lock knows them (settings aside: they are in the keys)."""
    return [reader(k) for k in READERS] + [card_step(""), alias_step("")]


def check() -> None:
    check_lock(all_steps(), LOCK)


# ---- running them -----------------------------------------------------------------------

def extract_key(step: Step, row: dict) -> str:
    return step.key(sha256=row["sha256"])


#: Memory one reader process may take (a large workbook in openpyxl, a long PDF), for
#: sizing the pool to the machine: 8 readers on a 4 GB runner were killed for memory.
READER_BYTES = 1024**3


def reader_workers(cap: int = 8) -> int:
    """Readers to run at once: no more than the CPUs, nor than the memory available holds."""
    try:
        with open("/proc/meminfo") as fh:
            avail = next(int(line.split()[1]) * 1024 for line in fh if line.startswith("MemAvailable:"))
    except (OSError, StopIteration, ValueError):
        avail = cap * READER_BYTES
    return max(1, min(cap, os.cpu_count() or 1, avail // READER_BYTES))


def _read_all(todo, body, read, workers: int, keep) -> list:
    """Read `todo` in a pool; `keep(key, result)` each as it finishes. Returns what a dead
    reader process left unread (a pool loses every pending job when one of its processes dies)."""
    left = {k: (r, s) for k, r, s in todo}
    try:
        with cf.ProcessPoolExecutor(workers) as ex:
            futs = {ex.submit(read, s.settings["kind"], body(r), s.settings.get("page_selector"),
                              s.settings.get("title_suffix") or ""): k for k, r, s in todo}
            for f in cf.as_completed(futs):
                k = futs[f]
                keep(k, f.result())
                left.pop(k)
    except cf.process.BrokenProcessPool:
        pass
    return [(k, r, s) for k, (r, s) in left.items()]


def extract(cur: Curation, fetch, jobs: list[tuple[dict, Step]], workers: int | None = None) -> dict[str, dict]:
    """{key: result} for each (fetch row, reader step): stored results as they are, the
    rest read from the bytes (fetched this run, else the originals store) and stored. A
    format this machine cannot read gives no result, and so does a file whose reader
    dies on it even alone (out of memory): it is named, the run goes on, and the next
    refresh tries it again."""
    from canopy_kb.prep.extract import extract_bytes

    keys = {extract_key(s, r): (r, s) for r, s in jobs}
    out = cur.results(keys)
    todo = [(k, r, s) for k, (r, s) in keys.items() if k not in out]
    if not todo:
        return out
    workers = workers or reader_workers()
    log(f"extract: {len(todo)} files to read ({len(out)} already read), {workers} at a time")
    fetch.prefetch([r for _, r, _ in todo])
    done = [0]

    def keep(k, res):
        done[0] += 1
        if done[0] % 500 == 0:
            log(f"  read {done[0]}/{len(todo)}")
        if res.get("unavailable"):
            return
        out[k] = res
        cur.put(k, res)

    left = _read_all(todo, fetch.body, extract_bytes, workers, keep)
    if left:
        log(f"extract: a reader died with {len(left)} files unread; reading those one at a time")
    for job in left:  # alone, in a process of its own: only a file that kills its reader is lost
        if _read_all([job], fetch.body, extract_bytes, 1, keep):
            log(f"  could not read {job[1].get('url', job[0][:12])}: its reader died (out of memory?); skipped")
    fetch.forget_bodies()
    return out


def card_inputs(it: dict, linked: list[str]) -> dict:
    """An item's card is the reading of what its prompt shows: its text (by sha), title,
    type, url, date and its referrers' titles."""
    return dict(text=it["sha256"], title=it["title"], type=it["type"], url=it["url"], date=it.get("date", ""),
                linked=linked)


def card_key(step: Step, it: dict, linked: list[str]) -> str:
    return step.key(**card_inputs(it, linked))


def card_keys(step: Step, it: dict, linked: list[str]) -> list[str]:
    """`card_key` first, then the addresses the same card had under the step's `formerly`."""
    return step.keys(**card_inputs(it, linked))


def card_tasks(step: Step, todo: list[tuple[str, dict, list[str]]]) -> list[dict]:
    """The engine's tasks for (key, item with its text, referrers' titles): one prompt each."""
    publisher, kinds = step.settings["publisher"], step.settings["kinds"]
    return [{"id": k, "item": it["id"], "title": it["title"], "url": it["url"],
             "prompt": _cards.prompt(it, linked, publisher, kinds)} for k, it, linked in todo]


def cards(cur: Curation, step: Step, todo: list[tuple[str, dict, list[str]]] | None = None, *,
          tasks: list[dict] | None = None, engine=None) -> dict[str, dict]:
    """{key: card} for `todo` (or tasks `card_tasks` made), by `engine` (Vertex if none):
    each checked to the card's shape and stored as it returns. A task with no result is
    asked again by the next refresh."""
    engine = engine or _engine.Vertex()
    tasks = tasks if tasks is not None else card_tasks(step, todo or [])
    return engine.run(step, tasks, lambda t, raw: _cards.check(raw), lambda k, card: cur.put(k, card, now=True))


def aliases(cur: Curation, step: Step, all_cards: list[dict], budget: Budget | None = None, *,
            engine=None) -> tuple[str, dict]:
    """(key, groups): the stored groups for these names (under this key or a former one),
    else the engine's (Vertex if none)."""
    p = _aliases.prompts(all_cards, step.settings["publisher"], step.settings["renames"], step.settings["apart"])
    keys = step.keys(prompts=[p[k][1] for k in sorted(p)])
    have = cur.results(keys)
    for k in keys:
        if k in have:
            if k != keys[0]:
                cur.put(keys[0], have[k])
            return keys[0], have[k]
    engine = engine or _engine.Vertex()
    tasks = [{"id": kind, "prompt": prompt, "names": len(rows)} for kind, (rows, prompt) in p.items()]
    got = engine.run(step, tasks, lambda t, raw: _aliases.groups(p[t["id"]][0], raw), lambda k, v: None)
    missing = sorted(set(p) - set(got))
    if missing:
        raise CurationError(f"aliases: no groups for {', '.join(missing)} from the {engine.name} engine; "
                            "the cards are stored, refresh again")
    for kind in sorted(got):
        log(f"aliases: {kind}: {len(got[kind])} groups over the top {len(p[kind][0])} names")
    cur.put(keys[0], got, now=True)
    return keys[0], got


if __name__ == "__main__":
    if "--relock" in sys.argv:
        print(write_lock(all_steps(), LOCK))
    else:
        check()
        print("steps.lock describes the code")
