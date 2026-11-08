"""Sharing your laptop sessions in a project's repo with that project's team.

RETIRED for new shares; the shares that already exist keep working until their
person turns them off.

A runner-discovered session is its box owner's alone (`access`, leg 3). A
`ProjectSessionShare` is the person's explicit, revocable choice, per project:
while it is live, every laptop session of theirs in the project's repo and
workspace is visible — read, write and pull — to the project's team (its owner
and the people taking part in it), never to an agent's own login
(`access._project_share_q`, leg 7).

Nobody may start a new one: the team it shares with is not a list anyone
chose — canopy adds a project's participants by itself, as people reply on its
tasks, back it, comment on its storyboards or join its check-ins — so turning a
project on handed edit and pull rights over your machine's sessions to people
who joined later. Sharing one session with named people is its page's "Add
people" (leg 2).

The routes are `/api/me/session-sharing/` (`sharing_api.py`): list your live
shares and turn one off. Each change is an audited setting change.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.agents.models import AgentProject, ProjectParticipant

from . import lifecycle
from .models import ProjectSessionShare


def live(user):
    return ProjectSessionShare.objects.filter(user=user, revoked_at__isnull=True)


def shared_projects(user):
    """The projects the person still shares their laptop sessions with. Never a
    personal one: a share on a project that became personal grants nothing (leg 7
    asks for a workspace project) and its name is not theirs to see."""
    return (AgentProject.objects.filter(pk__in=live(user).values("project_id"),
                                        visibility=AgentProject.WORKSPACE)
            .select_related("workspace").order_by("workspace_id", "handle"))


def team_size(project) -> int:
    """How many people the team is: its owner and the people taking part in it
    who have a canopy account."""
    ids = set(ProjectParticipant.objects.filter(project=project, person__user__isnull=False)
              .values_list("person__user_id", flat=True))
    if project.owner_user_id:
        ids.add(project.owner_user_id)
    return len(ids)


def stop_sharing(user, project) -> None:
    """Turn a live share off. The next read drops the sessions it covered."""
    with transaction.atomic():
        if live(user).filter(project=project).update(revoked_at=timezone.now()):
            lifecycle.record(lifecycle.SHARING_CHANGED, None, by=user, workspace_id=project.workspace_id,
                             change={"field": "project_share", "user_id": user.pk, "project": project.pk,
                                     "before": True, "after": False})
