"""Sharing your laptop sessions in a project's repo with that project's team.

A runner-discovered session is its box owner's alone (`access`, leg 3). The way
to make it multiplayer for a whole project, rather than one session at a time,
is this: per person × project, the person's own explicit, revocable choice
(`ProjectSessionShare`). Once on, every laptop session of theirs in the
project's repo and workspace is visible — read, write and pull — to the
project's team (its owner and the people taking part in it), never to an
agent's own login (`access._project_share_q`, leg 7).

The routes are `/api/me/session-sharing/` (`sharing_api.py`); each change is an
audited setting change.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.agents.models import AgentProject, ProjectParticipant
from apps.harness.models import ProjectOptIn, Runner
from apps.workspaces import services as wsvc

from .models import ProjectSessionShare


def _held_repos(user) -> set[str]:
    """The repos the person works in through canopy: what their boxes hold that
    they opted into (`Runner.project_names`), plus the repos they named."""
    held = set(ProjectOptIn.for_user_id(user.pk).projects or [])
    for runner in Runner.objects.filter(owner=user).exclude(status=Runner.RETIRED):
        held.update(runner.project_names())
    return {r.lower() for r in held if r}


def candidates(user):
    """The projects the person could share with: projects in their workspaces
    whose repo is one of theirs, plus any they already share."""
    slugs = wsvc.user_workspace_slugs(user)
    repos = _held_repos(user)
    q = Q(pk__in=live(user).values("project_id"))
    if repos:
        repo_q = Q()
        for r in repos:
            repo_q |= Q(repo_slug__iexact=r)
        q |= repo_q
    return (AgentProject.objects.filter(workspace_id__in=slugs).exclude(repo_slug="")
            .filter(q).select_related("workspace").order_by("workspace_id", "handle"))


def live(user):
    return ProjectSessionShare.objects.filter(user=user, revoked_at__isnull=True)


def team_size(project) -> int:
    """How many people the team is: its owner and the people taking part in it
    who have a canopy account."""
    ids = set(ProjectParticipant.objects.filter(project=project, person__user__isnull=False)
              .values_list("person__user_id", flat=True))
    if project.owner_user_id:
        ids.add(project.owner_user_id)
    return len(ids)


def set_shared(user, project, shared: bool) -> bool:
    """Turn sharing on or off. Returns whether it is on afterwards. The caller has
    checked the project is in one of the person's workspaces."""
    with transaction.atomic():
        current = live(user).filter(project=project).first()
        if shared and current is None:
            ProjectSessionShare.objects.create(user=user, project=project)
        elif not shared and current is not None:
            live(user).filter(project=project).update(revoked_at=timezone.now())
    return shared
