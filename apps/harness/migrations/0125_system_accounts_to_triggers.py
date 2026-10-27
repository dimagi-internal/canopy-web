"""Convert every workspace system account into the agent triggers it amounted to.

docs/superpowers/specs/2026-10-27-agent-triggers-design.md § C8. For each
`SystemAccount`, with each of its `SystemSender` bindings:

* the agents = every agent whose turns it initiated (`Turn.agent`, or the session's
  agent for a mail thread) — where its mail actually routed. None: this migration
  RAISES, naming the account; it never guesses which agent an alarm should wake;
* one `inbound_mail` trigger per (agent, sender): the account's name, the sender's
  address and subject pattern, prompt "", `turn_mode = manual` (a system account was
  at most an editor, whose turns ran manual), enabled unless the account was
  disabled, created by the account's creator, else the workspace's first owner;
* its past turns are rewritten to a trigger turn's shape (system initiator,
  `trigger:<id>`, accountable creator, `origin_ref.trigger_id`), so they stay
  attributed once `workspaces/0015` deletes the account's user.

An account with no sender binding fires nothing and becomes nothing.
"""
from __future__ import annotations

import re

from django.db import migrations


def _owner_of(apps, workspace_id):
    Membership = apps.get_model("workspaces", "WorkspaceMembership")
    row = (Membership.objects.filter(workspace_id=workspace_id, role="owner")
           .order_by("joined_at", "pk").first())
    return row.user_id if row else None


def _unique_name(AgentSchedule, agent_id, name):
    base, n = name[:190], 2
    while AgentSchedule.objects.filter(agent_id=agent_id, name=name).exists():
        name = f"{base} #{n}"
        n += 1
    return name


def _matches(trigger, ref):
    sender = str(ref.get("from") or "").strip().lower()
    if "<" in sender:
        sender = sender.rpartition("<")[2].rstrip(">").strip()
    if sender != trigger.sender_address:
        return False
    try:
        return not trigger.subject_pattern or bool(
            re.search(trigger.subject_pattern, str(ref.get("subject") or "")))
    except re.error:
        return False


def forwards(apps, schema_editor):
    SystemAccount = apps.get_model("workspaces", "SystemAccount")
    SystemSender = apps.get_model("workspaces", "SystemSender")
    Turn = apps.get_model("harness", "Turn")
    AgentSchedule = apps.get_model("harness", "AgentSchedule")
    Agent = apps.get_model("agents", "Agent")

    for account in SystemAccount.objects.order_by("pk"):
        senders = list(SystemSender.objects.filter(account_id=account.pk).order_by("pk"))
        turns = list(Turn.objects.filter(initiator_user_id=account.user_id)
                     .select_related("chat_session"))
        agent_of = {t.pk: t.agent_id or (t.chat_session.agent_id if t.chat_session_id else None)
                    for t in turns}
        agent_ids = {a for a in agent_of.values() if a is not None}
        if not senders:
            continue
        label = (f"system account {account.pk} {account.name!r} (user {account.user_id}, "
                 f"workspace {account.workspace_id})")
        if not agent_ids:
            raise RuntimeError(
                f"{label}: no turn it started says which agent its mail wakes, so it cannot "
                "be converted to a trigger without guessing. Create the inbound-mail trigger "
                "on the right agent by hand, delete the system account, and deploy again.")
        creator = account.created_by_id or _owner_of(apps, account.workspace_id)
        if creator is None:
            raise RuntimeError(f"{label}: no creator recorded and its workspace has no owner")
        made = {}
        for agent in Agent.objects.filter(pk__in=agent_ids).order_by("pk"):
            for sender in senders:
                name = account.name if len(senders) == 1 else f"{account.name} ({sender.address})"
                made.setdefault(agent.pk, []).append(AgentSchedule.objects.create(
                    agent_id=agent.pk, kind="inbound_mail",
                    name=_unique_name(AgentSchedule, agent.pk, name[:200]),
                    prompt="", cron="", timezone="UTC",
                    sender_address=sender.address.strip().lower(),
                    subject_pattern=sender.subject_pattern or "",
                    turn_mode="manual", enabled=account.disabled_at is None,
                    notify=["inbox"], created_by_id=creator, updated_by_id=creator,
                ))
        for turn in turns:
            options = made.get(agent_of[turn.pk]) or []
            if not options:
                continue
            ref = turn.origin_ref if isinstance(turn.origin_ref, dict) else {}
            trigger = next((t for t in options if _matches(t, ref)), options[0])
            turn.initiator_kind = "system"
            turn.initiator_via = f"trigger:{trigger.pk}"
            turn.initiator_assurance = "internal"
            turn.initiator_user_id = creator
            turn.origin_ref = {**ref, "trigger_id": trigger.pk, "trigger_name": trigger.name}
            turn.save(update_fields=["initiator_kind", "initiator_via", "initiator_assurance",
                                     "initiator_user", "origin_ref"])


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0124_agent_triggers"),
        ("workspaces", "0014_drop_shared_1password_vault"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
