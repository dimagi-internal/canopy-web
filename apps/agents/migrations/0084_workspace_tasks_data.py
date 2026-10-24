"""Tasks and projects become the workspace's (design 2026-10-24).

Every workspace gets its task settings (a key prefix derived from its slug).
Every project gets its workspace (its owner's, else its agent's) and a handle
(a workspace project keeps its own; an agent project's comes from its name),
and its agent becomes its lead agent, member and the holder of its `P<n>`
alias. Every task gets its workspace (its project's, else its agent's), a
number there in creation order and the key that makes, and its agent becomes
its assignee and the holder of its `T<n>` alias. `position` becomes `rank`;
`confidence` is normalised.
"""
import re

from django.db import migrations
from django.utils.text import slugify

HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")


def derive_prefix(slug: str, taken: set[str]) -> str:
    # Same rule as `apps.agents.work.derive_prefix`, frozen here.
    letters = re.sub(r"[^A-Za-z0-9]", "", slug).upper() or "WS"
    base = letters[:3] if len(letters) >= 3 else (letters + "XXX")[:3]
    if base not in taken:
        return base
    for n in range(4, len(letters) + 1):
        if letters[:n] not in taken:
            return letters[:n]
    i = 2
    while f"{base}{i}" in taken:
        i += 1
    return f"{base}{i}"


def handle_for(name: str, fallback: str, taken: set[str]) -> str:
    base = slugify(name or "")[:56].strip("-")
    if not HANDLE_RE.match(base):
        base = slugify(fallback)[:56].strip("-")
    if not HANDLE_RE.match(base):
        base = "project"
    handle, i = base, 2
    while handle in taken:
        handle = f"{base[:52]}-{i}"
        i += 1
    taken.add(handle)
    return handle


def forwards(apps, schema_editor):
    Workspace = apps.get_model("workspaces", "Workspace")
    TaskSettings = apps.get_model("agents", "TaskSettings")
    AgentProject = apps.get_model("agents", "AgentProject")
    AgentTask = apps.get_model("agents", "AgentTask")

    taken = set(TaskSettings.objects.values_list("prefix", flat=True))
    settings = {s.workspace_id: s for s in TaskSettings.objects.all()}
    for ws in Workspace.objects.order_by("slug"):
        if ws.slug not in settings:
            prefix = derive_prefix(ws.slug, taken)
            taken.add(prefix)
            settings[ws.slug] = TaskSettings.objects.create(workspace=ws, prefix=prefix)

    handles: dict[str, set[str]] = {}
    projects = list(AgentProject.objects.select_related("agent").order_by("id"))
    # Workspace projects keep their handles, so claim those first.
    for p in projects:
        if p.scope == "workspace":
            handles.setdefault(p.owner_workspace_id, set()).add(p.handle.lower())
    for p in projects:
        p.workspace_id = p.owner_workspace_id or p.agent.workspace_id
        p.created_by_agent_id = p.agent_id
        if p.scope == "workspace":
            p.handle = p.handle.lower()
        else:
            p.handle = handle_for(p.name, f"{p.agent.slug}-{p.ext_id}",
                                  handles.setdefault(p.workspace_id, set()))
        p.save(update_fields=["workspace", "created_by_agent", "handle"])
        p.member_agents.add(p.agent_id)

    counters = {slug: s.last_number for slug, s in settings.items()}
    project_ws = dict(AgentProject.objects.values_list("id", "workspace_id"))
    for t in AgentTask.objects.select_related("agent").order_by("created_at", "id").iterator():
        ws = project_ws.get(t.project_id) or t.agent.workspace_id
        counters[ws] = counters.get(ws, 0) + 1
        t.workspace_id = ws
        t.number = counters[ws]
        t.key = f"{settings[ws].prefix}-{t.number}"
        t.created_by_agent_id = t.agent_id
        t.rank = float(t.position or 0)
        conf = (t.confidence or "").strip().lower()
        t.confidence = conf if conf in ("high", "medium", "low") else ""
        t.save(update_fields=["workspace", "number", "key", "created_by_agent", "rank", "confidence"])
    for slug, n in counters.items():
        TaskSettings.objects.filter(workspace_id=slug).update(last_number=n)


class Migration(migrations.Migration):
    dependencies = [("agents", "0083_workspace_tasks_schema")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
