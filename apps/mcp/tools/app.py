"""`app_query` / `app_act` / `app_tasks_query` / `app_tasks_act` — one workspace app, as the person asking.

The tools of an app entry (`apps/agents/app_entries.py`). **The app comes from a credential, never an
argument**: the caller token names its conversation's current turn, the turn is stamped with the entry it runs
in (`Turn.capability`), and the entry names its one app. So a session cannot point these at another app, and a
turn that is not confined to an app entry reaches no app at all — a full-profile turn, a scoped token, read
mode, a host site, a PAT: each is refused.

**As the verified asker, with their own role.** Every call runs as the turn's initiator, who must be a canopy
person (never an agent's login or a contact), active, and proven by THIS message (`caller_context.is_verified`).
Their own app role decides what they may read and do (a guest's scoped role included), exactly as on the app's
own page. The writes (`app_act`, `app_tasks_act`) are the one narrow exception to "an agent turn's caller token
never writes" (docs/architecture/access-confinement-and-act-as.md § App entries): only the app's DECLARED
actions, only this app, recorded as "<agent> for <person>". Each call is one audit row naming the turn and the
app, never the rows read or the input written.
"""
from __future__ import annotations

from dataclasses import dataclass

from asgiref.sync import sync_to_async
from fastmcp.exceptions import ToolError

from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.server import mcp

NOT_CONFINED = ("the app tools exist only in a conversation confined to an app entry, where they act as the "
                "person who asked")
NOT_A_PERSON = ("the app tools act only for a verified person: this turn's asker is not one canopy could prove "
                "from this message, so nothing in the app is read or changed for them")


@dataclass(frozen=True)
class AppTurn:
    turn: object
    agent: object
    user: object
    workspace: str
    key: str

    @property
    def summary(self) -> str:
        return f"turn={self.turn.pk} app={self.workspace}/{self.key}"


def resolve(turn_id, user_id) -> AppTurn:
    """The app this call may reach and the person it acts as, from the caller token's own turn. Raises
    `app_entries.Refused` with words for the agent."""
    from apps.agents import app_entries, turn_context  # noqa: PLC0415 — as its neighbours: tools load before apps
    from apps.agents.models import Agent  # noqa: PLC0415 — as above
    from apps.harness import initiator  # noqa: PLC0415 — as above
    from apps.harness.caller_context import is_verified  # noqa: PLC0415 — as above
    from apps.harness.models import Turn  # noqa: PLC0415 — as above

    turn = (Turn.objects.select_related("agent", "chat_session__agent", "initiator_user")
            .filter(pk=turn_id).first()) if turn_id else None
    if turn is None or turn_context.was_refused(turn):
        raise app_entries.Refused(NOT_CONFINED)
    agent = turn.acting_agent
    key = app_entries.app_of_entry(getattr(agent, "interface", None), turn.capability) if agent else None
    if key is None:
        raise app_entries.Refused(NOT_CONFINED)
    user = turn.initiator_user if turn.initiator_kind == initiator.USER else None
    if (user is None or user.pk != user_id or not user.is_active or not is_verified(turn)
            or Agent.objects.filter(user=user).exists()):
        raise app_entries.Refused(NOT_A_PERSON)
    if not app_entries.installed(agent.workspace_id, key):
        raise app_entries.Refused(f"the app '{key}' is not installed or is switched off")
    return AppTurn(turn=turn, agent=agent, user=user, workspace=agent.workspace_id, key=key)


def _claims() -> dict:
    from apps.mcp.turn_scope import _turn_claims  # noqa: PLC0415 — as site.py: read per call

    claims = _turn_claims()
    # Only a CONFINED caller token: a scoped (full-profile) one stands for a turn that is not in an app entry,
    # and a sensitive turn's never lists these.
    if claims is None or claims.get("all_tools") or claims.get("sensitive"):
        raise ToolError(NOT_CONFINED)
    return claims


async def _run(tool: str, detail: str, call):
    """Resolve the app turn, run `call(app_turn)` in a thread, audit it either way."""
    from apps.agents import app_entries  # noqa: PLC0415 — as above: tools load before apps

    claims = _claims()
    uid = current_user_id()

    def work():
        at = resolve(claims.get("turn_id"), claims.get("user_id"))
        return at, call(at)

    try:
        at, out = await sync_to_async(work, thread_sensitive=True)()
    except (app_entries.Refused, ValueError) as exc:
        await write_audit(user_id=uid, tool=tool, args_summary=f"turn={claims.get('turn_id')} {detail}"[:500],
                          ok=False, error=str(exc)[:200])
        raise ToolError(str(exc)) from None
    await write_audit(user_id=uid, tool=tool, args_summary=f"{at.summary} {detail}"[:500], ok=True)
    return out


def _host():
    from apps.agents import app_entries  # noqa: PLC0415 — as above: tools load before apps

    host = app_entries.host()
    if host is None:
        raise app_entries.Refused("no workspace apps here")
    return host


@mcp.tool(annotations={"readOnlyHint": True})
async def app_query(collection: str = "", where: dict | None = None, order: str = "-created_at",
                    limit: int = 50) -> dict:
    """Read this conversation's app, as the person who asked.

    With no `collection`: what the app is and what they may do in it — the collections they may read (with
    their fields), the actions they may run and the task actions it declares. With a `collection`: its
    records they may read; `where` is field equalities (plus `id`), `order` a field or `created_at`, newest
    first with a leading `-`.
    """
    def call(at):
        host = _host()
        if not collection:
            return host.describe(at.workspace, at.key, at.user)
        return {"records": host.query(at.workspace, at.key, at.user, collection=collection, where=where,
                                      order=order, limit=limit)}

    return await _run("app_query", f"collection={collection or '-'}", call)


@mcp.tool
async def app_act(action: str, record: int | None = None, input: dict | None = None,
                  params: dict | None = None) -> dict:
    """Run one of the app's declared actions as the person who asked, exactly as if they pressed it.

    Only an action their own app role may run (`app_query` with no collection lists them). `record` is the
    record it applies to, when it applies to one; `input` the fields it takes. It is recorded as you acting
    for them. Do it only when they asked for it.
    """
    return await _run("app_act", f"action={action} record={record}",
                      lambda at: _host().act(at.workspace, at.key, at.user, at.agent, action=action,
                                             record=record, input=input, params=params))


@mcp.tool(annotations={"readOnlyHint": True})
async def app_tasks_query(status: str = "", category: str = "", due_after: str = "", due_before: str = "",
                          template: str = "", recurring: str = "", q: str = "", limit: int = 100) -> dict:
    """The tasks of the one project this app declares, that the person who asked may read.

    `status`, `category`: comma lists. `due_after` / `due_before`: dates (YYYY-MM-DD). `recurring=true` keeps
    the recurring templates; `template` (a task key) keeps one template's instances.
    """
    filters = {"status": status, "category": category, "due_after": due_after, "due_before": due_before,
               "template": template, "recurring": recurring, "q": q, "limit": limit}
    return await _run("app_tasks_query", f"status={status or '-'}",
                      lambda at: {"tasks": _host().tasks_query(at.workspace, at.key, at.user, **filters)})


@mcp.tool
async def app_tasks_act(name: str, key: str | None = None, input: dict | None = None) -> dict:
    """Run one of the app's declared task actions (finish, reopen, comment, make this period's one, change
    the fields it allows, or add a task) as the person who asked, exactly as if they did it on the app's page.

    `name` is the declared action; `key` the task (none for one that makes a task); `input` what it takes.
    It never starts an agent's turn. Recorded in the task's history as you acting for them.
    """
    return await _run("app_tasks_act", f"action={name} task={key or '-'}",
                      lambda at: _host().tasks_act(at.workspace, at.key, at.user, at.agent, name=name, task=key,
                                                   input=input))
