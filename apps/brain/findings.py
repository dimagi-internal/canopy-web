"""An output's lifecycle with the people who act on it, and folding a repeat into the one already open.

`BrainOutput.state` is the cascade's (active, withdrawn, stale) and nobody sets it by hand. `status` is the people's:
`open` when made, then `accepted` (someone took it up), `dismissed` (not worth acting on) or `done` (acted on). Who may
set it (`may_set`) is someone who sees the output (`outputs.visible_to`, as the person) AND acts for its audience: a
`system` finding's workspace's owners and admins (or an ancestor's, `perms.BRAIN_MANAGE`) or a canopy admin; a
`project` output's project readers who are members of its workspace; an `individual` or `memory` output's own person.
An admin agent in its own turn (`actor.Actor`, `actor_may_set`) sets a `system` finding's where it and the person its
turn answers to manage learning, and a `project` output's where it reads the project and they manage learning there or
lead or administer the project; never an output about a person. Each change is an event in the output's workspace
naming who made it (and for an agent, its turn and its person).

A run names what an output IS with a `key` (`brain_emit`). An emit whose kind and key match an output of the same
learner, subject and workspace that is still active and open or accepted is a repeat (`match`): it folds in (`fold`),
counting `seen`, taking the newer words, and adding this run's reads to the lineage, so a removal of any session either
run read still reaches it. A dismissed or done one does not take a repeat: that is news, and it is a new output."""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.agents.models import AgentProject
from apps.agents.services.projects import may_read_project
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from .models import ABOUT_A_PERSON, PROJECT, SYSTEM, BrainOutput, BrainRun

logger = logging.getLogger(__name__)

OPEN, ACCEPTED, DISMISSED, DONE = "open", "accepted", "dismissed", "done"
STATUSES = (OPEN, ACCEPTED, DISMISSED, DONE)
#: A repeat folds only into one still being worked on.
FOLDS_INTO = (OPEN, ACCEPTED)


def match(run: BrainRun, *, kind: str, key: str, workspace: str) -> BrainOutput | None:
    """The output a repeat of (`kind`, `key`) folds into, or None."""
    if not key:
        return None
    return (BrainOutput.objects.filter(capability_id=run.capability_id, workspace_id=workspace, kind=kind, key=key,
                                       subject_type=run.subject_type, subject=run.subject,
                                       state=BrainOutput.ACTIVE, status__in=FOLDS_INTO)
            .order_by("-pk").first())


def fold(out: BrainOutput, run: BrainRun, *, title: str, body: dict, cited) -> BrainOutput:
    """Take a repeat into `out`: one more sighting, the newer words, and lineage to what this run read."""
    from .emit import write_lineage  # noqa: PLC0415 — emit calls this module

    with transaction.atomic():
        locked = BrainOutput.objects.select_for_update().get(pk=out.pk)
        if locked.state != BrainOutput.ACTIVE:
            return locked                                        # withdrawn meanwhile: nothing to fold into
        locked.seen += 1
        locked.last_seen_at = timezone.now()
        locked.title, locked.body = title or locked.title, body
        locked.save(update_fields=["seen", "last_seen_at", "title", "body", "updated_at"])
        write_lineage(run, locked, cited=cited)
    return locked


def may_set(user, out: BrainOutput) -> bool:
    """May `user` (as themselves) change `out`'s status: they see it and act for its audience."""
    if not getattr(user, "is_authenticated", False) or out.state != BrainOutput.ACTIVE:
        return False
    from . import outputs  # noqa: PLC0415 — outputs reads this module's neighbours

    if not outputs.visible_to(user, as_person=True).filter(pk=out.pk).exists():
        return False
    return acts_for(user, out)


def actor_may_set(actor, out: BrainOutput) -> bool:
    """`may_set` for a management `actor.Actor`: the person as `may_set`; an admin agent only for a `system` or
    `project` output, by the management rule."""
    if not actor.is_agent:
        return may_set(actor.user, out)
    if out.state != BrainOutput.ACTIVE:
        return False
    cls = out.capability.purpose_class
    if cls == SYSTEM:
        return actor.may_manage(out.workspace_id)
    if cls == PROJECT:
        from .api_manage import may_manage_target  # noqa: PLC0415 — api_manage reads this module

        project = AgentProject.objects.filter(pk=out.subject).first() if out.subject.isdecimal() else None
        return (project is not None and acts_for(actor.user, out)
                and may_manage_target(actor, out.workspace_id, project))
    return False


def acts_for(user, out: BrainOutput, memo: dict | None = None) -> bool:
    """`may_set` for an output already known to be visible to `user` as the person (a list's rows): whether they act
    for its audience. `memo` keeps the per-workspace and per-project answers across a page."""
    memo = {} if memo is None else memo
    cls = out.capability.purpose_class
    if cls == SYSTEM:
        key = ("manage", out.workspace_id)
        if key not in memo:
            memo[key] = (bool(getattr(user, "is_superuser", False))
                         or perms.can(user, out.workspace_id, perms.BRAIN_MANAGE))
        return memo[key]
    if cls == PROJECT:
        key = ("project", out.workspace_id, out.subject)
        if key not in memo:
            project = AgentProject.objects.filter(pk=out.subject).first() if out.subject.isdecimal() else None
            memo[key] = (project is not None and wsvc.is_member(user, out.workspace_id)
                         and may_read_project(user, project))
        return memo[key]
    if cls in ABOUT_A_PERSON:
        return out.subject == str(user.pk)
    return False


def set_status(out: BrainOutput, status: str, *, by, acting: dict | None = None) -> BrainOutput:
    """Set `out`'s status as `by`; `acting` (`Actor.record`) names an agent, its turn and its person in the event."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if status == out.status:
        return out
    was = out.status
    BrainOutput.objects.filter(pk=out.pk).update(status=status, status_at=timezone.now(), status_by=by)
    out.refresh_from_db()
    _record(out, was, by, acting or {})
    return out


def _record(out: BrainOutput, was: str, by, acting: dict) -> None:
    from apps.events import services as events  # noqa: PLC0415 — as the other event writers of this app

    try:
        events.record([{"source": "brain.output", "kind": "output.status",
                        "summary": f"{out.capability.slug} output {out.pk}: {was} -> {out.status}",
                        "payload": {"output": out.pk, "from": was, "to": out.status, "by_id": getattr(by, "pk", None),
                                    **acting}}],
                      workspace=out.workspace)
    except Exception:  # noqa: BLE001 — a log line never fails the act
        logger.exception("could not record the status change of output %s", out.pk)
