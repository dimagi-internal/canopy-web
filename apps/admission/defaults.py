"""The rules every workspace starts with.

Seeded as ordinary rows: by migration 0002 into the workspaces that existed
then, and by `signals.py` into each workspace created since. From then on a
default is a row like any other — the workspace's admins edit, disable or
delete it at `/w/:workspace/settings/admission-rules`, and nothing re-seeds it.
"""
from __future__ import annotations

_NO_APPROVER = (
    "email from a caller who is not the owner runs in manual mode, and {where} has no one "
    "to approve the reply."
)

DEFAULTS: tuple[dict, ...] = (
    {
        "seed_key": "email-confined-manual-cloud",
        "origin": "email", "access": "confined", "turn_mode": "manual", "runner_kind": "cloud",
        "action": "reject", "notify_sender": True, "notify_owner": True, "enabled": True,
        "reason": _NO_APPROVER.format(where="a cloud session"),
    },
    {
        # Same dead end on a laptop unless the owner happens to open the hidden
        # session, so it ships as a row — but OFF: turning it on is one toggle.
        "seed_key": "email-confined-manual-laptop",
        "origin": "email", "access": "confined", "turn_mode": "manual", "runner_kind": "emdash",
        "action": "reject", "notify_sender": True, "notify_owner": True, "enabled": False,
        "reason": _NO_APPROVER.format(where="a laptop session nobody is watching"),
    },
    # The two refusals canopy always makes, as rows (canopy-web#1416, #1400 item 6).
    # A row cannot let these turns run (`AdmissionRule.is_refusal`); it decides
    # what the refusal records and who is told. Both ship with every notice OFF,
    # which is exactly what these refusals did before they were rows — turning
    # one on is the toggle.
    {
        "seed_key": "blocked-sender",
        "origin": "email", "sender": "blocked",
        "action": "reject", "notify_sender": False, "notify_owner": False, "enabled": True,
        "reason": "the sender is blocked in this workspace, so their mail is not run.",
    },
    # Turns no runner will take (canopy-web#1416 follow-up). Ordinary rules, not
    # refusals: nothing about the PERSON is wrong, so an admin may switch either
    # off and let such turns sit queued instead. Both tell the owner; neither
    # auto-replies (the sender of a chat, Slack or MCP ask reads the reason on
    # the turn itself).
    {
        "seed_key": "no-runner-paused",
        "reach": "paused",
        "action": "reject", "notify_sender": False, "notify_owner": True, "enabled": True,
        "reason": ("every runner that could take this is paused with no time set to resume, "
                   "so it would sit queued until someone noticed."),
    },
    {
        "seed_key": "no-runner-offline",
        "reach": "offline", "wait_hours": 24,
        "action": "reject", "notify_sender": False, "notify_owner": True, "enabled": True,
        "reason": "no runner that could take this has been reachable for a day.",
    },
    {
        "seed_key": "offered-nothing",
        "access": "none",
        "action": "reject", "notify_sender": False, "notify_owner": False, "enabled": True,
        "reason": ("the agent offers this person nothing: they are not one of its "
                   "admins, and nothing on its access list lets them in."),
    },
)


def seed(workspace_id: str, rule_model=None, keys=None) -> int:
    """Create the missing defaults for one workspace. Idempotent by `seed_key`,
    and a default someone DELETED is not brought back by a later call only
    because nothing calls this for an existing workspace again.

    `keys` limits it to those defaults: a later migration seeds only what it
    adds, so a default a workspace deleted is not brought back by it.

    Takes the model so the migration can pass its historical one. Uses
    `bulk_create`, which skips `save()`'s validation: the defaults are known to
    be valid, and a historical model has no `clean()` to call anyway.
    """
    if rule_model is None:
        from .models import AdmissionRule as rule_model
    have = set(rule_model.objects.filter(workspace_id=workspace_id)
               .exclude(seed_key="").values_list("seed_key", flat=True))
    rows = [rule_model(workspace_id=workspace_id, **d) for d in DEFAULTS
            if d["seed_key"] not in have and (keys is None or d["seed_key"] in keys)]
    rule_model.objects.bulk_create(rows)
    return len(rows)
