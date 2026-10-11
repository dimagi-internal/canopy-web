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
)


def seed(workspace_id: str, rule_model=None) -> int:
    """Create the missing defaults for one workspace. Idempotent by `seed_key`,
    and a default someone DELETED is not brought back by a later call only
    because nothing calls this for an existing workspace again.

    Takes the model so the migration can pass its historical one. Uses
    `bulk_create`, which skips `save()`'s validation: the defaults are known to
    be valid, and a historical model has no `clean()` to call anyway.
    """
    if rule_model is None:
        from .models import AdmissionRule as rule_model
    have = set(rule_model.objects.filter(workspace_id=workspace_id)
               .exclude(seed_key="").values_list("seed_key", flat=True))
    rows = [rule_model(workspace_id=workspace_id, **d) for d in DEFAULTS if d["seed_key"] not in have]
    rule_model.objects.bulk_create(rows)
    return len(rows)
