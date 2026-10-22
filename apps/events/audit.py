"""The settings audit trail — one plumbing for every settings change.

Every REST route that changes a setting says so at the route —
`@gate(..., setting=True)` (`apps/api/route_gates.py`) — and `audited`
(registered once on the API in `apps/api/api.py`) wraps each of them. Because the web UI, the `canopy` CLI,
scripts and MCP tools all reach a setting through those routes — an MCP tool IS a
route, called in-process — every change from every entry point lands here, as one
`SettingChange` row naming who, through what, the request (secrets redacted) and,
where the handler reports it, the state before and after.

    The change and its record commit together. `audited` runs the route inside
    one transaction and writes the row inside it, so a row that fails to write
    rolls the change back: there is no unrecorded change.

Before and after come with every setting route without a line of its own: while
the route runs, each model row it saves or deletes is recorded as one change,
`"<app_label>.<model>:<pk>"` with the fields that changed (`apps/api/change_capture.py`). A handler
that knows better reports with `note_change` (routing does,
`apps/harness/routing_audit.py`), and its notes replace the captured rows. When
every change it noted was a no-op — re-sending a rule unchanged — nothing is
recorded. A refused write (an HttpError, or a 4xx/5xx `Status`) records nothing
either.

Reading: `GET /api/events/setting-changes` — a workspace admin (`logs.read`) sees
their workspace's changes, and anyone sees the changes they made themselves.
"""
from __future__ import annotations

import contextvars
import datetime
import decimal
import functools
import inspect
import re
import uuid
from typing import Any

from django.db import models, transaction

from apps.api import change_capture
from apps.api.route_gates import gate_of

#: Body keys whose VALUES are withheld. Matched against each key, at any depth.
_SECRET_KEY = re.compile(
    r"(secret|token|password|passwd|credential|key$|_key|keys$|private|"
    r"vault|cookie|refresh|bearer|signing|webhook_?url|dsn|plaintext|"
    # `values` is the body of PUT /credentials ({"values": {ref: secret}}),
    # whose refs are arbitrary names (`canopy-pat`) the patterns above miss.
    r"^values$)",
    re.IGNORECASE,
)
REDACTED = "[redacted]"
_MAX_STR = 2000

#: Model fields whose stored values are withheld, beyond `_SECRET_KEY`: ciphertext
#: (`*_enc`) and anything that hashes or salts a secret.
_SECRET_FIELD = re.compile(r"(_enc$|hash|digest|salt|cipher)", re.IGNORECASE)
#: Rows a setting route writes that are not settings: the audit's own tables, the
#: logs, and the rolling session (written on every request).
_NOT_SETTINGS = frozenset({
    "events.settingchange", "events.activity", "events.event", "events.sessionusage",
    "mcp.mcpauditlog", "agents.agentcredentialread", "sessions.session",
})
#: At most this many captured rows per request; the rest are counted.
_MAX_CAPTURED = 100

_notes: contextvars.ContextVar[list | None] = contextvars.ContextVar("canopy_audit_notes", default=None)


# ---- what a handler tells the audit ----

def note_change(*, scope: str, before: Any, after: Any, workspace=None, summary: str = "") -> bool:
    """Report what this request changed, for the audit row the route will write.

    Call it from a route declared `setting=True`, inside the write. Returns False
    when nothing changed. Outside an audited route it does nothing (and returns
    whether anything changed), so a helper shared with unaudited code is safe.
    `workspace` overrides the one `audited` infers from the path; `summary`
    overrides the default one-line description.
    """
    changed = before != after
    notes = _notes.get()
    if notes is not None:
        notes.append({"scope": scope, "before": before, "after": after, "changed": changed,
                      "workspace": workspace, "summary": summary})
    return changed


# ---- what a route wrote, when it reports nothing ----

def _plain(field, value):
    """A stored value as the row records it (`change_capture`): secrets
    withheld, JSON-safe."""
    secret = (_SECRET_KEY.search(field.name) or _SECRET_FIELD.search(field.name)
              or isinstance(field, models.BinaryField))
    # A secret-named flag or count is not a secret (`Runner.private`).
    if secret and value not in (None, "", b"") and not isinstance(value, (bool, int, float)):
        return REDACTED
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, decimal.Decimal, datetime.timedelta)):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return REDACTED
    return redact(value)


def _captured(capture: change_capture.Capture) -> list[dict]:
    return [{**n, "changed": True, "workspace": None, "summary": ""} for n in capture.result()]


# ---- the wrapper ----

def operation_id(func) -> str:
    """The route's operationId — its explicit `operation_id=`, else the same rule
    `CanopyNinjaAPI` uses."""
    from apps.api.api import _app_label, api

    explicit = api.explicit_operation_ids().get(inspect.unwrap(func))
    if explicit:
        return explicit
    name = func.__name__
    if name in api._shared_view_names():
        return f"{_app_label(func.__module__)}_{name}"
    return name


def audited(func):
    """Ninja operation decorator (mode "operation": after validation). A no-op
    for every route not declared `setting=True`; for those, runs the route and
    writes its `SettingChange` in one transaction."""

    @functools.wraps(func)
    def wrapper(request, *args, **kwargs):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return func(request, *args, **kwargs)
        declared = gate_of(func)
        if declared is None or not declared.setting:
            return func(request, *args, **kwargs)
        op = operation_id(func)

        notes: list = []
        capture = change_capture.Capture(plain=_plain, skip=_NOT_SETTINGS, limit=_MAX_CAPTURED)
        token = _notes.set(notes)
        try:
            with transaction.atomic():
                with change_capture.capturing(capture):
                    result = func(request, *args, **kwargs)
                # The handler's own notes win; else what it wrote.
                if not _refused(result) and (not notes or any(n["changed"] for n in notes)):
                    _record(request, op, kwargs, notes or _captured(capture))
        finally:
            _notes.reset(token)
        return result

    return wrapper


def _refused(result) -> bool:
    from ninja import Status

    if isinstance(result, Status):
        return result.status_code >= 400
    if isinstance(result, tuple) and result and isinstance(result[0], int):
        return result[0] >= 400
    status = getattr(result, "status_code", None)
    return isinstance(status, int) and status >= 400


# ---- the row ----

def _record(request, op: str, kwargs: dict, notes: list) -> None:
    from apps.common import request_context
    from apps.events.models import SettingChange

    ctx = getattr(request, "provenance_context", None) or request_context.current()
    cred = ctx.get("credential") or {}
    user = getattr(request, "user", None)
    user = user if getattr(user, "is_authenticated", False) else None
    path_args, body = _split(kwargs)
    target = " / ".join(str(v) for v in path_args.values())
    changed = [n for n in notes if n["changed"]]
    who = getattr(user, "email", "") or cred.get("label") or "someone"
    summary = next((n["summary"] for n in changed if n["summary"]), "") or (
        f"{who} {_verb(request.method)} {op}" + (f" on {target}" if target else "")
    )
    workspace = next((n["workspace"] for n in changed if n["workspace"] is not None), None) \
        or _workspace_for(request, path_args)
    row = SettingChange.objects.create(
        workspace=workspace,
        actor=user,
        actor_email=getattr(user, "email", "") or "",
        via=_via(ctx, cred),
        credential={k: cred.get(k) for k in ("type", "id", "label") if cred.get(k) is not None},
        client=ctx.get("client", "")[:200],
        user_agent=ctx.get("user_agent", "")[:200],
        mcp_tool=ctx.get("mcp_tool", "")[:128],
        request_id=ctx.get("request_id", "")[:128],
        operation=op,
        method=request.method,
        path=request.path[:500],
        target=target[:300],
        summary=summary[:500],
        request={"path": path_args, "body": redact(body)},
        changes=[{"scope": n["scope"], "before": n["before"], "after": n["after"],
                  **({"also_deleted": n["also_deleted"]} if n.get("also_deleted") else {})}
                 for n in changed],
    )
    if workspace is not None:
        from . import activity

        activity.emit(activity.SETTING_CHANGED, workspace=workspace, actor=user,
                      surface=row.via, ref=f"sc{row.pk}", meta={"operation": op}, at=row.created_at)


def _verb(method: str) -> str:
    return {"POST": "ran", "PUT": "set", "PATCH": "changed", "DELETE": "removed"}.get(method, method.lower())


def _via(ctx: dict, cred: dict) -> str:
    if ctx.get("via_mcp"):
        return "mcp"
    kind = (cred.get("type") or "").lower()
    if kind == "session":
        return "ui"
    if "runner" in kind:
        return "runner"
    client = (ctx.get("client") or ctx.get("user_agent") or "").lower()
    if "canopy-cli" in client:
        return "cli"
    return "api" if kind else "unknown"


def _split(kwargs: dict) -> tuple[dict, Any]:
    """Path/query arguments (plain values) apart from the body (schema objects)."""
    path: dict = {}
    body: dict = {}
    for key, value in kwargs.items():
        if hasattr(value, "model_dump"):
            body[key] = value.model_dump(mode="json")
        elif isinstance(value, (str, int, float, bool)) or value is None or hasattr(value, "hex"):
            path[key] = str(value) if value is not None else None
        else:
            body[key] = value
    if len(body) == 1:
        body = next(iter(body.values()))
    return path, body


def redact(value: Any) -> Any:
    """`value` with every secret-named key's value withheld, at any depth."""
    if isinstance(value, dict):
        return {k: (REDACTED if _SECRET_KEY.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str) and len(value) > _MAX_STR:
        return value[:_MAX_STR] + "…"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_STR]


def _workspace_for(request, path_args: dict):
    """The tenant whose setting this is, from the request and its path. None
    for a person's own settings (`SettingChange.workspace`)."""
    from apps.workspaces.models import Workspace

    def ws(slug):
        return Workspace.objects.filter(slug=slug).first() if slug else None

    pinned = getattr(request, "workspace_slug", None)
    path = request.path
    if "workspace" in path_args:
        return ws(path_args["workspace"])
    if path.startswith("/api/workspaces/") and "slug" in path_args:
        return ws(path_args["slug"])
    if path.startswith("/api/agents/") and "slug" in path_args:
        from apps.agents.models import Agent

        qs = Agent.objects.filter(slug=path_args["slug"])
        if pinned:
            qs = qs.filter(workspace__slug=pinned)
        agent = qs.select_related("workspace").first()
        if agent is not None:
            return agent.workspace
    if "runner_id" in path_args:
        from apps.harness.models import Runner

        runner = Runner.objects.filter(pk=path_args["runner_id"]).select_related("workspace").first()
        if runner is not None and runner.workspace_id:
            return runner.workspace
    return ws(pinned)


# ---- reading ----

def visible_to(request):
    """The rows the caller may read — the MCP call log's rule
    (`apps/events/api.py::list_mcp_calls`): every change in a workspace where
    they hold `logs.read`, plus every change they made themselves. A
    NULL-workspace row (a person's own setting) is only ever its actor's. A
    request pinned to `/api/w/{ws}/` sees only that workspace's rows."""
    from django.db.models import Q

    from apps.events.models import SettingChange
    from apps.workspaces import permissions as perms

    scope = Q(workspace__slug__in=perms.request_read_slugs_with(request, perms.LOGS_READ))
    pinned = getattr(request, "workspace_slug", None)
    mine = Q(actor=request.user) & (Q(workspace__slug=pinned) if pinned else Q())
    return SettingChange.objects.filter(scope | mine)
