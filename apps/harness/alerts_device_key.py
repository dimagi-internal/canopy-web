"""Alert source `device_key_pending`: a runner's new device key waits for a person.

A box registering a device key starts it `pending`. Until someone who may trust
keys on that runner approves it (one passkey touch, under Settings -> Your
machines), the box cannot read the secrets of agents that need an approved
machine and their turns there fail — so this is `blocking`.

Resolves when the key stops being pending (approved or revoked): the next
reconcile no longer reports it. `approve` is a link to the existing approve deep
link (`api_device_keys.approve_path`); taking it does not close the alert by
itself, the approval does.

This replaces the "approve <runner>" mail `device_key_notify.notify_pending`
sent for a NEW key: the alert's own blocking notice reaches the same people
(those who hold `approve`). Private runners (`workspace_id` NULL) are skipped:
an alert needs a workspace (`Alert.workspace` is NOT NULL), so their pending-key
mail stays. A re-approval (an active key with a new ECDH key) is not `pending`
and also stays a mail.
"""
from __future__ import annotations

import uuid

from apps.attention.alerts import DISMISS, Action, Raise

from .api_device_keys import approve_path
from .models import Runner, RunnerDeviceKey
from .routing import agents_routed_to
from .runners import may_trust_device_keys

KIND = "device_key_pending"
SEVERITY = "blocking"
CONFIRM_WITHIN = None
CLOSING_ACTION_IDS = ("approve", "dismiss")


def reconcile(now) -> list[Raise]:
    out: list[Raise] = []
    keys = (RunnerDeviceKey.objects.filter(status=RunnerDeviceKey.PENDING, runner__workspace_id__isnull=False)
            .exclude(runner__status=Runner.RETIRED).select_related("runner").order_by("created_at"))
    for key in keys:
        runner = key.runner
        slugs = agents_routed_to(runner).values_list("slug", flat=True)
        out.append(Raise(
            subject_key=f"device_key:{key.pk}",
            workspace=runner.workspace_id,
            title=f"A new device key on {runner.name} is waiting for approval",
            url=approve_path(key),
            scopes=(f"runner:{runner.pk}", f"workspace:{runner.workspace_id}",
                    *[f"agent:{s}" for s in slugs]),
            body=(key.label or "").strip(),
            fingerprint=str(key.pk),
        ))
    return out


def _key_of(alert):
    kind, _, pk = alert.subject_key.partition(":")
    if kind != "device_key":
        return None
    try:
        pk = uuid.UUID(pk)
    except ValueError:
        return None
    return RunnerDeviceKey.objects.filter(pk=pk).select_related("runner", "runner__owner").first()


def actions(alert, user) -> list[Action]:
    if not getattr(user, "is_authenticated", False):
        return []
    key = _key_of(alert)
    if key is None or not may_trust_device_keys(user, key.runner):
        return []
    return [Action("approve", "Approve key", True, href=approve_path(key)), DISMISS]


def perform(alert, user, action_id) -> None:
    """No server-side handlers: `approve` is a link and `dismiss` the framework's."""
    raise ValueError(f"device_key_pending has no handler for {action_id!r}")
