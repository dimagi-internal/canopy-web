"""The sensitive-data vocabulary: the runner flag and conversation requirement `zdr`
becomes `sensitive_data`, and a capability's `data: phi` becomes `data: sensitive`
(spec 2026-10-22-sensitive-data-agents). Every stored copy is rewritten: declared
flags, a box's reported data mode, sessions' and tokens' requirements, and agents'
published interfaces (the parsed mapping and the YAML as saved)."""
import re

from django.db import migrations

OLD, NEW = "zdr", "sensitive_data"
_DATA_PHI = re.compile(r"^(\s*[\"']?data[\"']?\s*:\s*)[\"']?phi[\"']?(\s*(#.*)?)$", re.M)


def _swap(values):
    if not isinstance(values, list) or OLD not in values:
        return values, False
    return sorted({NEW if v == OLD else v for v in values}), True


def forwards(apps, schema_editor):
    RunnerFlag = apps.get_model("harness", "RunnerFlag")
    RunnerFlag.objects.filter(flag=OLD).update(flag=NEW)

    Runner = apps.get_model("harness", "Runner")
    for runner in Runner.objects.filter(capabilities__has_key="data_mode").only("pk", "capabilities"):
        caps = dict(runner.capabilities or {})
        if caps.get("data_mode") == OLD:
            caps["data_mode"] = NEW
            Runner.objects.filter(pk=runner.pk).update(capabilities=caps)

    Session = apps.get_model("canopy_sessions", "Session")
    for s in Session.objects.filter(metadata__has_key="runner_requirements").only("pk", "metadata"):
        meta = dict(s.metadata or {})
        meta["runner_requirements"], changed = _swap(meta.get("runner_requirements"))
        if changed:
            Session.objects.filter(pk=s.pk).update(metadata=meta)

    for model, field in (("AppCredential", "last_runner_requirements"),
                         ("DelegatedToken", "runner_requirements"),
                         ("ContactToken", "runner_requirements"),
                         ("HostGrant", "runner_requirements")):
        Model = apps.get_model("tokens", model)
        for row in Model.objects.exclude(**{field: []}).only("pk", field).iterator():
            value, changed = _swap(getattr(row, field))
            if changed:
                Model.objects.filter(pk=row.pk).update(**{field: value})

    Agent = apps.get_model("agents", "Agent")
    for agent in Agent.objects.exclude(interface={}).only("pk", "interface", "interface_source"):
        iface = agent.interface or {}
        caps = iface.get("capabilities") if isinstance(iface, dict) else None
        if not isinstance(caps, dict) or not any(
                isinstance(c, dict) and c.get("data") == "phi" for c in caps.values()):
            continue
        for cap in caps.values():
            if isinstance(cap, dict) and cap.get("data") == "phi":
                cap["data"] = "sensitive"
        source = _DATA_PHI.sub(r"\1sensitive\2", agent.interface_source or "")
        Agent.objects.filter(pk=agent.pk).update(interface=iface, interface_source=source)


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0107_runner_directed"),
        ("canopy_sessions", "0048_message_sealed_encrypted"),
        ("tokens", "0035_oauth_server_moved_out"),
        ("agents", "0076_kb_versions_sources"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
