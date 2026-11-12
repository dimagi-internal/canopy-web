"""What an agent's app entry reaches of a workspace app (`apps/agents/app_entries.py::Host`).

Registered with the framework in `apps.WorkspaceAppsConfig.ready`. Every call is the person's own: their app
roles (`access.roles_for`, a guest's scoped role included) decide, through the same services an app's page
calls (`services.query` / `services.act`, `tasks.query` / `tasks.act_task`), on the LIVE app only — never a
draft's test copy, never a switched-off app. A write names the agent acting for them, so history says
"<agent> for <person>". Nothing here checks who the person is: the caller (`apps/mcp/tools/app.py`) has
already proven them from the turn.
"""
from __future__ import annotations

import datetime as dt

from apps.agents import app_entries
from apps.workspaces import permissions as perms

from . import access, services
from . import tasks as app_tasks
from .models import AppInstallation

LIVE = services.LIVE
#: What one app query hands an agent at most.
MAX_ROWS = 200


def _inst(workspace: str, key: str) -> AppInstallation:
    inst = (AppInstallation.objects.select_related("live__template_version")
            .filter(workspace_id=workspace, key=key, enabled=True).first())
    if inst is None:
        raise app_entries.Refused(f"the app '{key}' is not installed or is switched off")
    return inst


def _roles(inst: AppInstallation, user) -> set[str]:
    roles = access.roles_for(user, inst)
    if roles <= {"public"}:
        # Not a member and not a guest of this app: as on its pages, the app is not there for them.
        raise app_entries.Refused(f"the person asking has no role in the app '{inst.key}'")
    return roles


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except services.Refused as exc:
        raise app_entries.Refused(str(exc)) from exc


def _date(value: str, name: str):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} is a date, YYYY-MM-DD") from None


class Host:
    """`app_entries.Host` for canopy's workspace apps."""

    def installed(self, workspace: str, key: str) -> bool:
        return AppInstallation.objects.filter(workspace_id=workspace, key=key, enabled=True).exists()

    def available(self, workspace: str) -> list[str]:
        return list(AppInstallation.objects.filter(workspace_id=workspace).order_by("key")
                    .values_list("key", flat=True))

    def name(self, workspace: str, key: str) -> str:
        inst = AppInstallation.objects.filter(workspace_id=workspace, key=key).only("name").first()
        return inst.name if inst is not None else key

    def may_open(self, user, workspace: str, key: str) -> bool:
        inst = AppInstallation.objects.filter(workspace_id=workspace, key=key, enabled=True).first()
        if inst is None:
            return False
        return perms.can(user, workspace, perms.READ) or access.is_guest_of(user, inst)

    def describe(self, workspace: str, key: str, user) -> dict:
        inst = _inst(workspace, key)
        roles = _roles(inst, user)
        m = services.manifest_for(inst, LIVE)
        collections = {name: {"label": c.get("label", name),
                              "fields": {f: {x: c["fields"][f][x] for x in ("type", "label", "options")
                                             if x in c["fields"][f]}
                                         for f in services.readable(c, roles)}}
                       for name, c in m["collections"].items() if services.may_read(c, roles)}
        actions = {name: {"label": a["label"], "collection": a["collection"], "op": a["op"], "input": a["input"],
                          "params": a.get("params") or {}}
                   for name, a in m["actions"].items()
                   if a.get("enabled", True) and a["role"] in roles}
        declared = app_tasks.declared(m)
        tasks = None
        if declared is not None:
            tasks = {"read": declared["read"] in roles,
                     "actions": {n: {"do": a["do"], "fields": a.get("fields") or []}
                                 for n, a in declared["actions"].items() if a["role"] in roles}}
        return {"app": {"workspace": workspace, "key": key, "name": inst.name},
                "your_roles": sorted(roles - {"public"}), "collections": collections, "actions": actions,
                "tasks": tasks}

    def query(self, workspace: str, key: str, user, *, collection: str, where: dict | None, order: str,
              limit: int) -> list[dict]:
        inst = _inst(workspace, key)
        roles = _roles(inst, user)
        if where is not None and not isinstance(where, dict):
            raise ValueError("where is an object of field equalities")
        return _call(services.query, inst, roles=roles, collection=collection, where=where or {},
                     order=order or "-created_at", limit=max(1, min(int(limit or 50), MAX_ROWS)), space=LIVE,
                     user=user)

    def act(self, workspace: str, key: str, user, agent, *, action: str, record: int | None,
            input: dict | None, params: dict | None) -> dict:
        inst = _inst(workspace, key)
        roles = _roles(inst, user)
        return _call(services.act, inst, roles=roles, action=action, record_id=record, input=input or {},
                     user=user, space=LIVE, params=params, acting_agent=agent)

    def tasks_query(self, workspace: str, key: str, user, *, status: str = "", category: str = "",
                    due_after: str = "", due_before: str = "", template: str = "", recurring: str = "",
                    q: str = "", limit: int = 100) -> list[dict]:
        inst = _inst(workspace, key)
        if recurring not in ("", "true", "false"):
            raise ValueError("recurring is true or false")
        return _call(app_tasks.query, inst, user, status=status, category=category,
                     due_after=_date(due_after, "due_after"), due_before=_date(due_before, "due_before"),
                     template=template, recurring=recurring, q=q, limit=limit)

    def tasks_act(self, workspace: str, key: str, user, agent, *, name: str, task: str | None,
                  input: dict | None) -> dict:
        inst = _inst(workspace, key)
        out, made = _call(app_tasks.act_task, inst, user, name, task, input, on_behalf_by_agent=agent)
        return {"task": out, "made": made}
