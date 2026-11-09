"""Agent roles become permission bundles: viewer -> read_only, user -> capability lines.

The ladder `owner > admin > editor > user > viewer` is replaced by named bundles of permissions.
What an old line meant
is kept, not guessed:

    editor lines        -> editor (unchanged)
    viewer lines        -> read_only (it opens `ask`), PLUS one capability line for every OTHER
                           capability that was open to viewers, so a site's visitors keep their door.
                           If the agent's `ask` was a user-level capability, the viewer line gets no
                           role (read_only would newly open `ask`): its capability lines only.
    user lines          -> one capability line for every capability that role could open (all of
                           them); with nothing published the line admitted nobody to anything and
                           goes
    workspace defaults  -> the same, over the capabilities of the agents in that workspace and below
                           (a viewer default only for names that were viewer-level everywhere)
    capability `role:`  -> removed from the stored interface; the interface source is regenerated
    manual_only         -> carried over to every line that came from a line

Each affected agent's (and workspace's) old lines, interface and source are kept in the settings
audit (`SettingChange`, operation `access_roles_migration`), as `0112` did.
"""
import yaml
from django.db import migrations

_OPENS_VIEWER = ("viewer",)
_OPENS_USER = ("viewer", "user")
_SOURCE_FIELDS = ("kind", "user_id", "subject_workspace_id", "at_least", "include_below", "domain",
                  "verified", "note", "granted_by_id")


def _old_role(cap):
    return cap.get("role") if cap.get("role") in ("viewer", "user") else "user"


def _snapshot(line):
    return {"id": line.pk, "subject_key": line.subject_key, "role": line.role,
            "capability": line.capability, "manual_only": line.manual_only}


def _capability_lines(line, role, caps, *, ask_is_user_level):
    """[(role, capability)] the new lines for one old line, given {name: old role} `caps`."""
    if line.role == "viewer":
        extra = [n for n, r in sorted(caps.items()) if r == "viewer" and n != "ask"]
        keep_role = "" if (ask_is_user_level and "ask" in caps) else "read_only"
        return ([(keep_role, "")] if keep_role else []) + [("", n) for n in extra]
    if line.role == "user":
        return [("", n) for n in sorted(caps)]
    return [(line.role, "")]


def _rewrite(Grant, line, wanted):
    """Replace `line` by the lines in `wanted` (role, capability); the first reuses its row."""
    if not wanted:
        line.delete()
        return
    base = line.subject_key.partition("#")[0]
    for i, (role, capability) in enumerate(wanted):
        key = f"{base}#use:{capability}" if capability else base
        if i == 0:
            line.role, line.capability, line.subject_key = role, capability, key
            if capability:
                line.manual_only = False
            line.save(update_fields=["role", "capability", "subject_key", "manual_only"])
            continue
        Grant.objects.update_or_create(
            agent_id=line.agent_id, workspace_id=line.workspace_id, subject_key=key,
            defaults=dict(role=role, capability=capability,
                          manual_only=line.manual_only and not capability,
                          **{f: getattr(line, f) for f in _SOURCE_FIELDS}))


def forwards(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    Grant = apps.get_model("agents", "AgentGrant")
    Workspace = apps.get_model("workspaces", "Workspace")
    Change = apps.get_model("events", "SettingChange")

    agents = list(Agent.objects.all())
    caps_of = {a.pk: {n: _old_role(c) for n, c in ((a.interface or {}).get("capabilities") or {}).items()}
               for a in agents}

    # --- each agent's own lines ---------------------------------------------------------
    for agent in agents:
        caps = caps_of[agent.pk]
        lines = list(Grant.objects.filter(agent=agent, role__in=("viewer", "user")))
        before = [_snapshot(ln) for ln in lines]
        ask_user_level = caps.get("ask") == "user"
        for ln in lines:
            _rewrite(Grant, ln, _capability_lines(ln, ln.role, caps, ask_is_user_level=ask_user_level))
        iface = agent.interface or {}
        stored = iface.get("capabilities") or {}
        has_role = any("role" in c for c in stored.values())
        if not (lines or has_role):
            continue
        changes = []
        if lines:
            after = [_snapshot(ln) for ln in Grant.objects.filter(agent=agent).order_by("id")]
            changes.append({"scope": f"access-list:{agent.slug}", "changed": True,
                            "before": before, "after": after})
        if has_role:
            new_iface = {**iface, "capabilities": {n: {k: v for k, v in c.items() if k != "role"}
                                                   for n, c in stored.items()}}
            old_source = agent.interface_source or ""
            changes.append({"scope": f"interface:{agent.slug}", "changed": True,
                            "before": {"interface": iface, "source": old_source},
                            "after": {"interface": new_iface}})
            agent.interface = new_iface
            agent.interface_source = yaml.safe_dump(new_iface, sort_keys=False) if old_source.strip() else ""
            agent.save(update_fields=["interface", "interface_source"])
        Change.objects.create(
            workspace_id=agent.workspace_id, actor=None, actor_email="", via="system",
            operation="access_roles_migration", method="MIGRATE", path="agents/0118", target=agent.slug,
            summary=f"{agent.slug}: viewer/user lines became read_only and capability lines; "
                    "old lines and interface kept here"[:500],
            changes=changes)

    # --- workspace defaults -------------------------------------------------------------
    parents = dict(Workspace.objects.values_list("slug", "parent_id"))

    def below(slug):
        out, frontier = {slug}, {slug}
        while frontier:
            frontier = {s for s, p in parents.items() if p in frontier} - out
            out |= frontier
        return out

    for ln in list(Grant.objects.filter(agent__isnull=True, role__in=("viewer", "user"))):
        scope = below(ln.workspace_id)
        levels: dict[str, set[str]] = {}
        for a in agents:
            if a.workspace_id in scope:
                for name, role in caps_of[a.pk].items():
                    levels.setdefault(name, set()).add(role)
        # A name counts as viewer-level only when it was viewer-level for every agent that has it.
        caps = {n: ("viewer" if r == {"viewer"} else "user") for n, r in levels.items()}
        ask_user_level = caps.get("ask") == "user"
        before = [_snapshot(ln)]
        _rewrite(Grant, ln, _capability_lines(ln, ln.role, caps, ask_is_user_level=ask_user_level))
        after = [_snapshot(x) for x in Grant.objects.filter(workspace_id=ln.workspace_id).order_by("id")]
        Change.objects.create(
            workspace_id=ln.workspace_id, actor=None, actor_email="", via="system",
            operation="access_roles_migration", method="MIGRATE", path="agents/0118",
            target=ln.workspace_id,
            summary=f"{ln.workspace_id}: a default viewer/user line became read_only and capability "
                    "lines; old line kept here"[:500],
            changes=[{"scope": f"agent-defaults:{ln.workspace_id}", "changed": True,
                      "before": before, "after": after}])


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0117_grant_capability_and_subtree"),
        ("events", "0009_session_usage_workspace_required"),
        ("workspaces", "0017_superuser_role_and_workspace_hold"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
