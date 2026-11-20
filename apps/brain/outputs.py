"""Who sees what a learning feature produced (`BrainOutput`): by its class's audience, never wider.

* `individual`, about a person: that person, their coaches (the same rule as reading their sessions,
  `access.review_grantors`: an agent coach only inside a running turn that answers to them), and their
  manager (`reporting.reports_of`, a person as themselves). Outputs, never a transcript.
* `memory`, about a person: that person. (The agents serving them read their HCP entries under their
  own grant; this is not that path.)
* `project`, about a project: whoever may read the project (`project_read_q`), within its workspace.
* `system`: canopy's superusers and the owners of the output's workspace.

A withdrawn output is seen by no one. One whose source was removed (`stale`) is, marked."""
from __future__ import annotations

from django.db.models import CharField, Q
from django.db.models.functions import Cast

from apps.agents.models import AgentProject
from apps.agents.services.projects import project_read_q
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import reporting
from .models import INDIVIDUAL, MEMORY, PROJECT, SYSTEM, BrainOutput


def visible_to(user):
    """The outputs `user` may read, as a queryset (workspace-scoped, as every read)."""
    if not getattr(user, "is_authenticated", False):
        return BrainOutput.objects.none()
    from apps.canopy_sessions import access  # noqa: PLC0415 — canopy_sessions imports this app's readers

    mine = wsvc.user_workspace_slugs(user)
    live = BrainOutput.objects.filter(workspace_id__in=mine).exclude(state=BrainOutput.WITHDRAWN)
    about_me = Q(subject_type="person", subject=str(user.pk))
    q = Q(capability__purpose_class__in=[INDIVIDUAL, MEMORY]) & about_me
    coached = [str(i) for i in access.review_grantors(user).values_list("user_id", flat=True)]
    if coached:
        q |= Q(capability__purpose_class=INDIVIDUAL, subject_type="person", subject__in=coached)
    reports = [str(i) for i in reporting.reports_of(user, mine)]
    if reports and not _is_agent_login(user):
        q |= Q(capability__purpose_class=INDIVIDUAL, subject_type="person", subject__in=reports)
    readable = (AgentProject.objects.filter(project_read_q(user)).annotate(key=Cast("pk", CharField()))
                .values("key"))
    q |= Q(capability__purpose_class=PROJECT, subject_type="project", subject__in=readable)
    owned = perms.slugs_with(user, perms.OWN)
    if getattr(user, "is_superuser", False):
        q |= Q(capability__purpose_class=SYSTEM)
    elif owned:
        q |= Q(capability__purpose_class=SYSTEM, workspace_id__in=owned)
    return live.filter(q).distinct()


def _is_agent_login(user) -> bool:
    from apps.agents.models import Agent  # noqa: PLC0415 — as the other lazy imports here

    return Agent.objects.filter(user_id=user.pk).exists()
