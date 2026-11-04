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
may import it, and it knows no model. So it also holds the registry of surfaces:
what a screen is lives in a product app, which connections may not import, so each
kind of surface registers how to find one and who may open it (`register_surface`,
from its `AppConfig.ready()`), and each kind of thing an agent's connection can be
shared with registers what is there to approve (`register_share_target`).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from . import calls

MAX_ENTRIES = 5
MAX_LINES = 50
METHODS = ("GET", "POST", "PATCH", "DELETE")
AS_VALUES = ("viewer", "shared")
#: An agent's slug as canopy-web makes one (a Django slug, at most 80 characters).
AGENT_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")

# One id-shaped segment standing in for `*` when a pattern is checked: it fits an
# id and an object name both, so a read's `*` may be either.
_PLACEHOLDER = "A00000000000000"
_ENTRY_KEYS = {"provider", "as", "agent", "reads", "writes"}


@dataclass(frozen=True)
class Surface:
    """The screen a call comes from, once the caller is known to be allowed to open it.
    `declared` is what THAT screen declares: a view's named revision, an app's live manifest.
    `revision` is what an audit line records: the one the page named.
    The connection comes from `workspace` and nowhere else."""
    kind: str
    ref: str  # what an audit line calls it, without the kind: a view's id, an app's key
    workspace: str
    revision: int | None
    declared: list[dict]
    #: The view's current declaration, when the page is on an older revision. A call must fit both:
    #: an old page never gets more than it drew, and a narrowing reaches it at once.
    current: list[dict] | None = None
    #: The project a view is in, by id; an app, and a view in none, have none.
    project: str | None = None

    def allows(self, provider: str, as_: str, agent_slug: str | None, match_result: calls.Match, method: str) -> bool:
        return all(allows(lists, provider, as_, agent_slug, match_result, method)
                   for lists in (self.declared, *(() if self.current is None else (self.current,))))

    @property
    def full_ref(self) -> str:
        """This screen and no other: an app's key means something only in its workspace."""
        return f"{self.workspace}/{self.ref}" if self.kind == "app" else self.ref

    @property
    def share_key(self) -> tuple[str, str] | None:
        """The `(kind, ref)` a share with this screen is under. None: nothing can be shared with it."""
        if self.kind == "app":
            return ("app", self.full_ref)
        return ("project", self.project) if self.project else None

    @property
    def current_declared(self) -> list[dict]:
        return self.declared if self.current is None else self.current

    def write_lines(self) -> list[dict]:
        """The writes this screen declares, each with whose connection it asks: on an older revision,
        those both declarations hold. Declared is not usable: an `as: shared` line needs its admin's
        approval too, which `call_service` knows and this doesn't."""
        lines = _writes(self.declared)
        if self.current is None:
            return lines
        now = _writes(self.current)
        return [line for line in lines if line in now]


@dataclass(frozen=True)
class ShareTarget:
    """What an agent's connection can be shared with, whoever is asking. `ref` is its canonical
    spelling, `declared` every entry declared there now: a project's holds each of its views'."""
    ref: str
    title: str
    workspace: str
    declared: list[dict]


_surfaces: dict[str, Callable[..., Surface]] = {}
_share_targets: dict[str, Callable[[str], ShareTarget | None]] = {}
_removal_listeners: list[Callable[[str, str], None]] = []


def register_surface(kind: str, resolve: Callable[..., Surface]) -> None:
    """`resolve(request, ref)` answers with a `Surface`, or raises what its own read route
    would for someone who may not open it. `ref` is the request's `surface` object."""
    _surfaces[kind] = resolve


def resolve_surface(request, ref: dict) -> Surface:
    return _surfaces[ref["kind"]](request, ref)


def register_share_target(kind: str, lookup: Callable[[str], ShareTarget | None]) -> None:
    """`lookup(ref)` answers with what is there, or None when nothing is. It checks no person: the
    caller has already decided who may ask."""
    _share_targets[kind] = lookup


def share_target(kind: str, ref: str) -> ShareTarget | None:
    return _share_targets[kind](ref)


def on_share_target_removed(listener: Callable[[str, str], None]) -> None:
    """`listener(kind, ref)` hears that a share target is gone for good. A ref is only a string, so
    without this the next thing to take its name would inherit what was approved for the last."""
    _removal_listeners.append(listener)


def share_target_removed(kind: str, ref: str) -> None:
    for listener in _removal_listeners:
        listener(kind, ref)


def _writes(declared: list[dict]) -> list[dict]:
    return [{"provider": e.get("provider"), "as": e.get("as"), "agent": e.get("agent"),
             "method": line.get("method"), "path": line.get("path")}
            for e in declared or [] for line in e.get("writes") or []]


def shared_lines(declared: list[dict], provider: str, agent_slug: str) -> tuple[list[dict], list[dict]]:
    """`(reads, writes)`: every line `declared` asks of this agent's shared connection, each once."""
    reads: list[dict] = []
    writes: list[dict] = []
    for e in declared or []:
        if e.get("provider") != provider or e.get("as") != "shared" or e.get("agent") != agent_slug:
            continue
        for lines, found in ((e.get("reads") or [], reads), (e.get("writes") or [], writes)):
            found.extend(line for line in lines if line not in found)
    return reads, writes


def same_lines(one: list[dict], other: list[dict]) -> bool:
    """The same lines, in whatever order."""
    return not not_covered(one, other) and not not_covered(other, one)


def not_covered(lines: list[dict], approved: list[dict]) -> list[dict]:
    """Those of `lines` an approval doesn't hold, by exact line: a `*` approved is not an id asked."""
    return [line for line in lines if line not in approved]


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
            if not isinstance(agent, str) or not AGENT_RE.fullmatch(agent):
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
