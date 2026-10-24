"""The data catalog: every read a dynamic view may ask the canopy page to make.

This list is the whole of what a view's code can reach. The frame has no network
of its own (`connect-src 'none'`); it can only ask the canopy page around it, and
the page answers a request only if it names a model here, with only the query
parameters listed for it. The page then GETs the route under
`/api/w/<the view's workspace>/` with the viewer's own session, so the route's own
gate decides what comes back. A view can show nothing its viewer could not
already read, and nothing outside its workspace.

Each entry is an existing REST read; nothing here re-implements one. Adding a
model is one entry. Leave out anything that reveals or changes a secret
(vaults, tokens, credentials, mail bodies, transcripts): the frame's author can
see what the view shows its viewers, so a read belongs here only if showing it
to the people who can open the view is fine.

`{workspace}` in a path is always the view's own workspace, filled in by the
page; every other `{name}` is supplied by the view and must match `PATH_PARAM_RE`.
The frontend reads this list from `GET /api/dynamic-views/guide/` and enforces it
(`frontend/src/dynamicViews/bridge.ts`).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

PATH_PARAM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")

_TASK_FILTERS = ("agent", "project", "status", "waiting", "assignee", "created_by", "label", "priority",
                 "milestone", "category", "kind", "due_before", "due_after", "q", "sort", "limit", "parent")


@dataclass(frozen=True)
class Model:
    name: str
    path: str
    description: str
    params: tuple[str, ...] = field(default_factory=tuple)

    @property
    def path_params(self) -> tuple[str, ...]:
        return tuple(p for p in re.findall(r"{(\w+)}", self.path) if p != "workspace")

    def as_dict(self) -> dict:
        out = asdict(self)
        out["params"] = list(self.params)
        out["path_params"] = list(self.path_params)
        return out


CATALOG: tuple[Model, ...] = (
    # Work
    Model("tasks", "/tasks/", "Tasks in the workspace, filtered (a page of rows).", _TASK_FILTERS),
    Model("task_count", "/tasks/count/", "How many tasks match the same filters.",
          tuple(p for p in _TASK_FILTERS if p not in ("due_before", "due_after", "q", "sort", "limit", "milestone"))),
    Model("task", "/tasks/{key}/", "One task by key (e.g. CON-42), with its history."),
    Model("projects", "/workspace-projects/", "The workspace's projects.", ("status",)),
    Model("project", "/workspace-projects/{workspace}/{handle}/", "One project by handle."),
    Model("milestones", "/workspace-projects/{workspace}/{handle}/milestones/", "A project's milestones."),
    Model("project_updates", "/workspace-projects/{workspace}/{handle}/updates/", "A project's updates.",
          ("limit",)),
    Model("triage", "/triage/", "What waits for a decision: suggested tasks, ideas, proposed projects.",
          ("project", "limit")),
    Model("labels", "/labels/", "The workspace's task labels."),
    Model("saved_views", "/views/", "Saved task views (the viewer's own and the shared ones)."),
    Model("checkins", "/checkins/", "Project check-ins."),
    # Agents
    Model("agents", "/agent-roster/", "Every agent in the workspace, with what it is doing."),
    Model("agent", "/agents/{slug}/", "One agent, with counts."),
    Model("agent_turns", "/agents/{slug}/turns/", "An agent's recent turns.", ("limit",)),
    Model("agent_schedules", "/agents/{slug}/schedules/", "An agent's recurring schedules.", ("limit",)),
    Model("agent_skills", "/agents/{slug}/skills/", "An agent's skill catalog."),
    Model("agent_runs", "/agent-runs/", "Run documents across agents.",
          ("kind", "subject", "agent", "repo_slug", "active", "limit")),
    Model("schedule_week", "/agents/schedules/week", "A week of scheduled fires.", ("start", "mine")),
    # People and the workspace
    Model("workspace", "/workspaces/{workspace}/", "The workspace itself."),
    Model("members", "/workspaces/{workspace}/members/", "The workspace's members and their roles."),
    Model("analytics", "/workspaces/{workspace}/analytics", "Turns, chats, messages, tokens and people over time.",
          ("days", "tree")),
    Model("health", "/workspaces/{workspace}/health", "What is not set up right across the workspace."),
    Model("network", "/workspaces/{workspace}/network", "Who works with whom, through which threads.",
          ("days", "tree")),
    # Activity and conversation
    Model("timeline", "/timeline/", "The team activity feed.", ("subsystem", "limit", "before")),
    Model("huddles", "/huddles/", "Agent huddles.", ("agent", "limit")),
    Model("threads", "/threads/", "Bounded agent-to-agent threads.",
          ("parent_key", "parent_value", "agent", "status", "limit")),
    Model("sessions", "/canopy-sessions/", "Chat sessions the viewer can see (titles and state, no transcript).",
          ("state", "limit", "q", "repo", "since", "until")),
    # Artifacts
    Model("shareouts", "/shareouts/", "Work briefings.", ("date_from", "date_to", "project", "limit")),
    Model("walkthroughs", "/walkthroughs/", "Demo walkthroughs.", ("project", "kind", "mine")),
    Model("storyboards", "/storyboards/", "Storyboards."),
    Model("dynamic_views", "/dynamic-views/", "The workspace's dynamic views.", ("project",)),
)

BY_NAME: dict[str, Model] = {m.name: m for m in CATALOG}


def catalog() -> list[dict]:
    return [m.as_dict() for m in CATALOG]


def validate_sources(sources) -> list[dict]:
    """Normalise a view's declared `sources`, or raise ValueError naming what is wrong.

    The same rule the page applies to an on-demand read, applied once at save
    time, so a view that names an unknown model or parameter is refused when it
    is written rather than failing silently for every viewer."""
    if sources in (None, ""):
        return []
    if not isinstance(sources, list):
        raise ValueError("sources must be a list of {alias, model, params?, path?}")
    if len(sources) > 20:
        raise ValueError("a view may declare at most 20 sources")
    out: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(sources):
        if not isinstance(raw, dict):
            raise ValueError(f"sources[{i}] must be an object")
        alias = str(raw.get("alias") or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,39}", alias):
            raise ValueError(f"sources[{i}].alias must be an identifier")
        if alias in seen:
            raise ValueError(f"sources[{i}].alias '{alias}' is declared twice")
        seen.add(alias)
        model = BY_NAME.get(str(raw.get("model") or ""))
        if model is None:
            raise ValueError(f"sources[{i}].model must be one of: {', '.join(BY_NAME)}")
        params = raw.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError(f"sources[{i}].params must be an object")
        unknown = sorted(set(params) - set(model.params))
        if unknown:
            raise ValueError(f"sources[{i}]: {model.name} does not take {', '.join(unknown)}")
        path = raw.get("path") or {}
        if not isinstance(path, dict):
            raise ValueError(f"sources[{i}].path must be an object")
        # `project` may be omitted on a project view: the page fills it from the
        # view's own project (see bridge.ts), so the common case needs no handle.
        missing = [p for p in model.path_params if p not in path and p != "handle"]
        extra = sorted(set(path) - set(model.path_params))
        if missing or extra:
            raise ValueError(f"sources[{i}]: {model.name} needs path {list(model.path_params)}")
        for k, v in path.items():
            if not PATH_PARAM_RE.match(str(v)):
                raise ValueError(f"sources[{i}].path.{k} is not a valid identifier")
        entry = {"alias": alias, "model": model.name}
        if params:
            entry["params"] = {k: (v if isinstance(v, (bool, int, float)) else str(v)) for k, v in params.items()}
        if path:
            entry["path"] = {k: str(v) for k, v in path.items()}
        out.append(entry)
    return out
