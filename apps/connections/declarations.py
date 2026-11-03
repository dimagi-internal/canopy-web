"""What a view or app page declares it may ask a connection to do.

A screen that calls Salesforce through canopy-web says so up front, in a
`connections` declaration saved with the view (or in an app manifest):

    [{"provider": "salesforce", "as": "viewer" | "shared", "agent": "<slug>",
      "reads":  [{"method": "GET",   "path": "query"}],
      "writes": [{"method": "PATCH", "path": "sobjects/Lead/*"}]}]

`path` is a pattern in the canonical, decoded form `calls.match` returns: literal
segments, and `*` standing for exactly one segment. A write names its object
exactly (`sobjects/Lead/*`, never `sobjects/*/*`) and a flow or Apex write names
the exact flow or class; `*` is for ids in a write, and for any one segment in a
read. Every line is checked here, at save, against the allowlist (`calls`) and
against the list it is in; at run time `allows` checks the call that was made
against the same lines. The two checks are separate on purpose: saving can be
wrong, a run is where a token is used.

`as: "viewer"` calls with the viewer's own connection; `as: "shared"` with the
connection `agent` shared. One entry per (provider, as, agent).

This module imports only `calls`: the view and app code that saves a declaration
may import it, and it knows no model.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from . import calls

MAX_ENTRIES = 5
MAX_LINES = 50
METHODS = ("GET", "POST", "PATCH", "DELETE")
AS_VALUES = ("viewer", "shared")
#: An agent's slug as canopy-web makes one (a Django slug, at most 80 characters).
AGENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# One id-shaped segment standing in for `*` when a pattern is checked: it fits an
# id and an object name both, so a read's `*` may be either.
_PLACEHOLDER = "A00000000000000"
_ENTRY_KEYS = {"provider", "as", "agent", "reads", "writes"}


def _line(where: str, raw, kind: str) -> dict:
    """One normalised `{method, path}`, or ValueError naming `where` and the line."""
    if not isinstance(raw, dict) or set(raw) != {"method", "path"}:
        raise ValueError(f"{where} must be an object with a method and path")
    method, path = raw["method"], raw["path"]
    if not isinstance(method, str) or method.upper() not in METHODS:
        raise ValueError(f"{where}: method must be one of {', '.join(METHODS)}")
    if not isinstance(path, str):
        raise ValueError(f"{where}: path must be text")
    method = method.upper()
    shown = f"{where} ({method} {path[:80]})"
    return {"method": method, "path": path, "shown": shown, "kind": kind}


def _check_line(entry_provider: str, line: dict) -> dict:
    shown, kind, method, path = line["shown"], line["kind"], line["method"], line["path"]
    segments = path.split("/")
    probe = "/".join(_PLACEHOLDER if s == "*" else s for s in segments)
    try:
        found = calls.match(entry_provider, method, probe)
    except calls.NotAllowed as exc:
        raise ValueError(f"{shown}: {exc}") from None
    if found.path != probe:
        raise ValueError(f"{shown}: that is not a path this screen may call: write it plainly, not encoded")
    rule = found.rule
    if kind == "read" and rule.kind != "read":
        raise ValueError(f"{shown}: that is a write, not a read: declare it under writes")
    if kind == "write" and rule.kind != "write":
        raise ValueError(f"{shown}: that is a read, not a write: declare it under reads")
    if kind == "write":
        tokens = rule.pattern.split("/")
        for i, seg in enumerate(segments):
            if seg == "*" and not (i < len(tokens) and tokens[i] == "{id}"):
                raise ValueError(f"{shown}: a write names its object, flow or class exactly: "
                                 "only a record id may be *")
    return {"method": method, "path": path}


def validate(raw, *, agent_exists: Callable[[str], bool] | None = None) -> list[dict]:
    """A normalised copy of a `connections` declaration, or ValueError naming what is wrong.

    `agent_exists`, when given, says whether a slug is an agent in the saver's
    workspace; an app manifest is validated without one (there is no workspace
    yet) and the slug is only checked for shape. Run time resolves it either way."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("connections must be a list of connection declarations")
    if len(raw) > MAX_ENTRIES:
        raise ValueError(f"connections may have at most {MAX_ENTRIES} entries")
    out: list[dict] = []
    seen: set[tuple[str, str, str | None]] = set()
    for i, entry in enumerate(raw):
        at = f"connections[{i}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{at} must be an object")
        extra = sorted(str(k) for k in set(entry) - _ENTRY_KEYS)
        if extra:
            raise ValueError(f"{at} takes only provider, as, agent, reads and writes, not {', '.join(extra)}")
        provider = entry.get("provider")
        try:
            calls.match(provider, "GET", "query")  # only says whether the provider is supported
        except calls.NotSupported as exc:
            raise ValueError(f"{at}: {exc}") from None
        except calls.NotAllowed:
            pass
        as_ = entry.get("as", "viewer")
        if as_ not in AS_VALUES:
            raise ValueError(f"{at}: as must be 'viewer' or 'shared'")
        agent = entry.get("agent")
        if as_ == "viewer":
            if agent not in (None, ""):
                raise ValueError(f"{at}: an agent is only for as 'shared'; a viewer call uses the viewer's own")
            agent = None
        else:
            if not isinstance(agent, str) or not AGENT_RE.match(agent):
                raise ValueError(f"{at}: as 'shared' needs the agent (its slug) whose shared connection is used")
            if agent_exists is not None and not agent_exists(agent):
                raise ValueError(f"{at}: no agent '{agent}' in this workspace")
        key = (provider, as_, agent)
        if key in seen:
            raise ValueError(f"{at} declares {provider} as {as_}{' for ' + agent if agent else ''} twice")
        seen.add(key)
        lists: dict[str, list[dict]] = {}
        total = 0
        for name, kind in (("reads", "read"), ("writes", "write")):
            lines = entry.get(name)
            if lines in (None, ""):
                lines = []
            if not isinstance(lines, list):
                raise ValueError(f"{at}.{name} must be a list of {{method, path}}")
            total += len(lines)
            if total > MAX_LINES:
                raise ValueError(f"{at} may have at most {MAX_LINES} reads and writes in all")
            done: list[dict] = []
            for j, raw_line in enumerate(lines):
                line = _line(f"{at}.{name}[{j}]", raw_line, kind)
                checked = _check_line(provider, line)
                if checked in done:
                    raise ValueError(f"{line['shown']} is declared twice")
                done.append(checked)
            lists[name] = done
        item: dict = {"provider": provider, "as": as_}
        if agent:
            item["agent"] = agent
        item["reads"], item["writes"] = lists["reads"], lists["writes"]
        out.append(item)
    return out


def allows(declared: list[dict], provider: str, as_: str, agent_slug: str | None,
           match_result: calls.Match, method: str) -> bool:
    """Whether a call that passed `calls.match` is one the declaration names.

    `match_result` is the canonical `Match`, never a raw path. A write is covered
    only by a declared write and a read only by a declared read; `*` stands for
    one segment. Anything unexpected answers False."""
    if not isinstance(match_result, calls.Match) or not isinstance(method, str):
        return False
    rule = match_result.rule
    if method.upper() != rule.method:
        return False
    wanted_agent = agent_slug if as_ == "shared" else None
    entry = next((e for e in declared or [] if isinstance(e, dict) and e.get("provider") == provider
                  and e.get("as") == as_ and e.get("agent") == wanted_agent), None)
    if entry is None:
        return False
    segments = match_result.path.split("/")
    for line in entry.get("writes" if rule.kind == "write" else "reads") or []:
        if line.get("method") != rule.method:
            continue
        want = str(line.get("path")).split("/")
        if len(want) == len(segments) and all(w in ("*", s) for w, s in zip(want, segments)):
            return True
    return False
