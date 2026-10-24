"""Who does a model step's work: the AI session running the refresh, or Gemini on Vertex.

A curator's refresh decides WHAT needs a model (each new card, the alias groups when the
names moved) and prices it; an engine does it. Either way the prompt is the step's own
and the result is checked to the step's shape before it is stored, so a result does not
depend on who made it, and its key does not name them (`Step.meta`; Jonathan,
2026-10-24: "the model is just metadata").

* `Session`, the default: the AI session that started the refresh does the work. The
  refresh writes the tasks to a directory and waits; the session writes the results
  beside them and the refresh carries on, so one crawl serves every step. Judgment (the
  alias groups) stays with the session; a batch of cards fans out to fast subagents
  (`split`, then `merge`). A refresh on this engine is started by an AI session, never
  by a scheduler: nobody would answer it.
* `Vertex`: Gemini on Vertex AI (`gemini.py`, the model each step's `meta` names), for a
  cold build of a whole corpus, where a session would take hours. Needs a Google access
  token.

The protocol, in the session's directory, one step at a time (`<step>` is the step's name):

  <step>.tasks.json     {"step", "instructions", "count", "tasks": [{"id", "prompt", ...}],
                         "errors": {id: why the last result for it was refused}}
  <step>.results.json   written by the session: {id: result}. Write a temporary file and
                        rename it, so the refresh never reads half of one.
  <step>.done           written by the session to give up on what is left: those tasks get
                        no result this run, and the next refresh asks again.
  <step>.finished.json  written by the refresh once it has taken the results: {"stored", "left"}

A results file may answer some of the tasks: the refresh stores those, rewrites the tasks
file with the rest (and why any was refused), and waits again. `python -m
canopy_kb.prep.engine status|split|merge DIR` are the session's helpers.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from canopy_kb.prep.raw import log

#: (task, result as returned) -> result as stored, or ValueError.
Check = Callable[[dict, Any], Any]
#: (task id, checked result): store it the moment it is checked.
Keep = Callable[[str, Any], None]

#: Estimated USD per task on the session, for planning: a card by a fast subagent model
#: (~2.5k tokens in, ~0.4k out, plus its share of the subagent's overhead), the alias
#: groups by the session's own model (one long list per kind).
SESSION_COST = {"card": 0.005, "aliases": 0.75}

INSTRUCTIONS = {
    "card": (
        "Each task's prompt is complete: read it and return the JSON card it asks for (an object with "
        "summary, kind, orgs, programs, countries, period, superseded, answers), using only what its excerpt "
        "shows. A summary is required. These are many small, independent readings: fan them out to fast "
        "subagents (`split`, a part each; `merge` the parts' results) rather than reading them one by one."),
    "aliases": (
        "Each task is one kind of name (org, program) and its prompt lists the names with document counts. "
        "Return the JSON list of groups it asks for. This is judgment: do it yourself, carefully. A wrong "
        "merge (a charity and its parent, two countries' ministries) corrupts the entity index; a missed "
        "one only leaves two pages."),
}


class Vertex:
    """Gemini on Vertex AI: each task one generateContent call, in parallel."""

    name = "vertex"

    def __init__(self, workers: int = 32):
        self.workers = workers

    def cost(self, step) -> float:
        return step.cost

    def run(self, step, tasks: list[dict], check: Check, keep: Keep) -> dict[str, Any]:
        from canopy_kb.prep import gemini
        model, call = step.meta["model"], dict(step.meta.get("call") or {})
        out: dict[str, Any] = {}
        lock = threading.Lock()

        def job(t):
            try:
                raw, _ = gemini.generate_json(model, [{"text": t["prompt"]}], **call)
                value = check(t, raw)
            except Exception as e:  # noqa: BLE001 - one failed task; the next refresh asks again
                log(f"  {step.name} failed:", str(t.get("item") or t["id"])[:40], str(e)[:100])
                return
            with lock:
                keep(t["id"], value)
                out[t["id"]] = value

        if tasks:
            with cf.ThreadPoolExecutor(min(self.workers, len(tasks))) as ex:
                list(ex.map(job, tasks))
        return out


def _write(path: Path, obj) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _paths(d: Path, step: str) -> dict[str, Path]:
    return {"tasks": d / f"{step}.tasks.json", "results": d / f"{step}.results.json",
            "done": d / f"{step}.done", "finished": d / f"{step}.finished.json"}


class Session:
    """The AI session running the refresh: tasks out to `dir`, results back from it."""

    name = "session"

    def __init__(self, dir: Path, timeout: float = 4 * 3600, poll: float = 5.0):
        self.dir, self.timeout, self.poll = Path(dir), timeout, poll

    def cost(self, step) -> float:
        return SESSION_COST.get(step.name, step.cost)

    def run(self, step, tasks: list[dict], check: Check, keep: Keep) -> dict[str, Any]:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = _paths(self.dir, step.name)
        for f in p.values():
            f.unlink(missing_ok=True)
        pending = {t["id"]: t for t in tasks}
        out: dict[str, Any] = {}
        errors: dict[str, str] = {}
        deadline = time.monotonic() + self.timeout
        while pending:
            _write(p["tasks"], {"step": step.name, "instructions": INSTRUCTIONS.get(step.name, ""),
                                "count": len(pending), "tasks": list(pending.values()), "errors": errors})
            log(f"session: {len(pending)} {step.name} task(s) in {p['tasks']}; waiting for {p['results'].name}")
            got = self._wait(p, deadline)
            if got is None:
                break
            errors = {}
            for i, raw in got.items():
                if i not in pending:
                    continue
                try:
                    value = check(pending[i], raw)
                except ValueError as e:
                    errors[i] = str(e)
                    continue
                keep(i, value)
                out[i] = value
                pending.pop(i)
            log(f"session: {step.name}: {len(out)} stored, {len(pending)} left"
                + (f", {len(errors)} refused" if errors else ""))
        p["tasks"].unlink(missing_ok=True)
        _write(p["finished"], {"stored": len(out), "left": len(pending)})
        return out

    def _wait(self, p: dict[str, Path], deadline: float) -> dict | None:
        """The next results file ({id: result}), or None: the session gave up, or time ran out."""
        while time.monotonic() < deadline:
            if p["results"].exists():
                try:
                    got = json.loads(p["results"].read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    bad = p["results"].with_name(p["results"].name + ".bad")
                    os.replace(p["results"], bad)
                    log(f"session: {p['results'].name} is not JSON ({e}); moved to {bad.name}, still waiting")
                    continue
                p["results"].unlink(missing_ok=True)
                if isinstance(got, dict):
                    return got
                log(f"session: {p['results'].name} must be {{task id: result}}; ignored, still waiting")
                continue
            if p["done"].exists():
                log(f"session: {p['done'].name}: the session gave up on what is left")
                return None
            time.sleep(self.poll)
        log("session: timed out waiting for results")
        return None


# ---- the session's helpers -------------------------------------------------------------

def status(d: Path) -> dict:
    """Each step waiting on the session (its task count) and each one finished."""
    d = Path(d)
    out: dict[str, Any] = {"waiting": {}, "finished": {}}
    for f in sorted(d.glob("*.tasks.json")):
        if ".part-" in f.name:
            continue
        out["waiting"][f.name.split(".")[0]] = json.loads(f.read_text(encoding="utf-8"))["count"]
    for f in sorted(d.glob("*.finished.json")):
        out["finished"][f.name.split(".")[0]] = json.loads(f.read_text(encoding="utf-8"))
    return out


def split(d: Path, step: str, size: int = 40) -> list[Path]:
    """The step's tasks as parts of `size`, one per subagent: <step>.part-NNN.tasks.json.
    Each subagent writes <step>.part-NNN.results.json ({id: result}); `merge` combines them."""
    d = Path(d)
    tasks = json.loads(_paths(d, step)["tasks"].read_text(encoding="utf-8"))
    for old in d.glob(f"{step}.part-*.json"):
        old.unlink()
    out = []
    for n in range(0, len(tasks["tasks"]), size):
        f = d / f"{step}.part-{n // size + 1:03d}.tasks.json"
        chunk = tasks["tasks"][n:n + size]
        _write(f, {"step": step, "instructions": tasks["instructions"], "count": len(chunk), "tasks": chunk,
                   "errors": {t["id"]: tasks["errors"][t["id"]] for t in chunk if t["id"] in tasks.get("errors", {})},
                   "results": str(f.with_name(f.name.replace(".tasks.json", ".results.json")))})
        out.append(f)
    return out


def merge(d: Path, step: str) -> dict:
    """The parts' results as the step's results file, for the refresh to take."""
    d = Path(d)
    got: dict[str, Any] = {}
    bad = []
    for f in sorted(d.glob(f"{step}.part-*.results.json")):
        try:
            part = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            bad.append(f.name)
            continue
        got.update(part if isinstance(part, dict) else {})
    _write(_paths(d, step)["results"], got)
    for f in d.glob(f"{step}.part-*.json"):
        if f.name not in bad:
            f.unlink()
    return {"results": len(got), "unreadable_parts": bad}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m canopy_kb.prep.engine", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "split", "merge"):
        s = sub.add_parser(name)
        s.add_argument("dir", type=Path)
        if name != "status":
            s.add_argument("--step", default="card")
        if name == "split":
            s.add_argument("--size", type=int, default=40)
    a = ap.parse_args(argv)
    if a.cmd == "status":
        out: Any = status(a.dir)
    elif a.cmd == "split":
        out = [str(f) for f in split(a.dir, a.step, a.size)]
    else:
        out = merge(a.dir, a.step)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
