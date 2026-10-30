"""Every agent's "who may use it" is its access list; the rules it was published with are gone.

Before this, an agent's access was spread over the interface's `full:` rules, each capability's
`callers`, `editors:` and the implicit "a workspace editor is an editor" rule. This writes the
equivalent lines for any agent not already moved (`convert_access` / the Access screen did most),
gives every capability its `role`, strips the old keys from the stored interface, and regenerates
`interface_source` (an old source still carrying `callers:` would be refused on its next save).
Each agent's old source and interface are kept in the settings audit (`SettingChange`).

The translation is the one `convert_access` checked against every real interface:

    full: member[@d]               -> everyone in the agent's workspace [at d]      -> editor
    full: contact[@d][:verified]   -> a known contact [at d] [verified]             -> editor
    a capability's callers         -> the same lines                                -> viewer if it was
                                      open to contacts, else user
    the implicit editor rule       -> everyone in the workspace at least editor     -> editor, manual_only
    (unless `editors: confined`)
    unknown classes                -> no line (they would have admitted nobody established)
"""
import re

import yaml
from django.db import migrations

_CALLER = re.compile(r"^(?P<base>member|contact|unknown)"
                     r"(?:@(?P<domain>[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}))?"
                     r"(?::(?P<verified>verified))?$")
_RANK = {"viewer": 1, "user": 2, "editor": 3}
_STALE = ("full", "editors", "callers_default")


def _role_of(cap):
    if cap.get("role") in ("viewer", "user"):
        return cap["role"]
    callers = cap.get("callers") or []
    return "viewer" if any(c.startswith(("contact", "unknown")) for c in callers) else "user"


def _lines_for(agent, iface):
    """[(kind, role, domain, verified, at_least, manual_only)] for the rules in `iface`."""
    wanted = {}
    for cls in iface.get("full") or []:
        wanted[cls] = "editor"
    for cap in (iface.get("capabilities") or {}).values():
        role = _role_of(cap)
        for cls in cap.get("callers") or []:
            if _RANK[role] > _RANK.get(wanted.get(cls), 0):
                wanted[cls] = role
    out = {}
    for cls, role in wanted.items():
        m = _CALLER.match(cls)
        if not m or m["base"] == "unknown":
            continue
        domain, verified = m["domain"] or "", bool(m["verified"])
        if m["base"] == "member":
            key, line = ("workspace", "", domain), ("workspace", role, domain, False, "", False)
        else:
            key, line = ("contact", "", domain), ("contact", role, domain, verified, "", False)
        if key not in out or _RANK[role] > _RANK[out[key][1]]:
            out[key] = line
    if iface.get("editors") != "confined":
        out[("workspace", "editor", "")] = ("workspace", "editor", "", False, "editor", True)
    return list(out.values())


def _subject_key(kind, ws, at_least, domain):
    if kind == "workspace":
        return f"workspace:{ws}:{at_least}:{domain}"
    return f"contact:{domain}"


def forwards(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    Grant = apps.get_model("agents", "AgentGrant")
    Change = apps.get_model("events", "SettingChange")
    for agent in Agent.objects.all():
        iface = agent.interface or {}
        legacy = (any(k in iface for k in _STALE)
                  or any("callers" in c for c in (iface.get("capabilities") or {}).values()))
        if not agent.uses_access_list:
            for kind, role, domain, verified, at_least, manual in _lines_for(agent, iface):
                key = _subject_key(kind, agent.workspace_id, at_least, domain)
                Grant.objects.update_or_create(
                    agent=agent, subject_key=key,
                    defaults=dict(kind=kind, role=role, domain=domain, verified=verified,
                                  at_least=at_least, manual_only=manual,
                                  subject_workspace_id=agent.workspace_id if kind == "workspace" else None,
                                  note="moved from the interface's rules" if not manual
                                  else "was the implicit rule: a workspace editor is an editor"))
        if not (legacy or agent.uses_access_list):
            continue
        caps = {}
        for name, cap in (iface.get("capabilities") or {}).items():
            new = {k: v for k, v in cap.items() if k != "callers"}
            new["role"] = _role_of(cap)
            caps[name] = new
        new_iface = {"version": iface.get("version", 1), "capabilities": caps}
        old_source = agent.interface_source or ""
        Change.objects.create(
            workspace_id=agent.workspace_id, actor=None, actor_email="", via="system",
            operation="access_list_migration", method="MIGRATE", path="agents/0112",
            target=agent.slug,
            summary=f"{agent.slug}: access rules moved onto its access list; old interface kept here"[:500],
            changes=[{"scope": f"interface:{agent.slug}", "changed": True,
                      "before": {"interface": iface, "source": old_source},
                      "after": {"interface": new_iface}}])
        agent.interface = new_iface
        agent.interface_source = (yaml.safe_dump(new_iface, sort_keys=False) if old_source.strip() else "")
        agent.uses_access_list = True
        agent.save(update_fields=["interface", "interface_source", "uses_access_list"])


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0111_remove_agent_zk_enabled"),
        ("events", "0009_session_usage_workspace_required"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
