"""Who sees what a learning feature produced (`BrainOutput`): by its class's audience, never wider.

* `individual`, about a person: that person, their coaches (the same rule as reading their sessions,
  `access.review_grantors`: an agent coach only inside a running turn that answers to them; only outputs made in
  the workspaces the coach covers, `access.coach_scope`), and their
  manager: only the outputs made in the workspace where the reporting line is, and only to the manager as a
  PERSON (`as_person`, from `api_admin.manager_leg_open` at the route: their browser session or hand-minted
  PAT, never an agent, a turn's token, an act-as token, an OAuth or MCP client or a site's delegated token
  acting for them). Outputs, never a transcript.
* `memory`, about a person: that person. (The agents serving them read their HCP entries under their
  own grant; this is not that path.)
* `project`, about a project: whoever may read the project (`project_read_q`), within its workspace.
* `system`: canopy's superusers and the owners and admins of the output's workspace (`perms.BRAIN_MANAGE`), which for
  a run started for a workspace is that workspace (`emit`); an ancestor's owner or admin holds it too
  (`perms.slugs_with` counts inherited roles), a sub-workspace's or a sibling's does not. The owners' and admins' leg
  opens to the PERSON (`as_person`, as the manager leg) and to an admin agent in its own turn (`acting`, an
  `actor.Actor`: only where its login AND the person its turn answers to manage, `Actor.managed`); never to a stored
  agent token, a turn's caller token or a person's token over MCP.

Each leg opens only for a learner whose `audience` names it (`registry.CLASS_AUDIENCE`, which a registration may only
narrow): `person`, `coaches` and `manager` for `individual`; `project_readers` for `project`; for `system`,
`canopy_admins` is the superusers' leg and `agent_admins` the owners' and admins' (as the person and as an admin agent).
A `memory` output's own person always reads it: it is theirs (`serving_agents` is HCP's path, not this one). A learner
with no audience is read by no one; nothing here opens what its audience leaves out.

A withdrawn output is seen by no one, and neither is a `stale` one (a source of it was removed: its words are
emptied and it waits to be derived again from the rest). Outputs are derived, so they stay readable after a
workspace turns "Learning reads" off (`learning_reads`); nothing new is made from its sessions then."""
from __future__ import annotations

from django.db.models import CharField, Q
from django.db.models.functions import Cast

from apps.agents.models import AgentProject
from apps.agents.services.projects import project_read_q
from apps.canopy_sessions import access
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import participants, reporting
from .models import INDIVIDUAL, MEMORY, PROJECT, SYSTEM, BrainCapability, BrainOutput


def visible_to(user, *, as_person: bool = False, acting=None):
    """The outputs `user` may read, as a queryset (workspace-scoped, as every read). `as_person` says the
    request is the person themselves (`api_admin.manager_leg_open`); only then do the manager leg and the owners' and
    admins' `system` leg open. `acting` (an admin agent's `actor.Actor`, whose `user` is `user`) opens the `system` leg
    where it manages learning, and nothing else. Every leg but a person's own memory is its learners' only
    (`_heard_by`)."""
    if not getattr(user, "is_authenticated", False):
        return BrainOutput.objects.none()

    heard = _heard_by()

    def to(cls: str, word: str) -> Q:
        return Q(capability__purpose_class=cls, capability_id__in=heard.get((cls, word), ()))

    mine = wsvc.user_workspace_slugs(user)
    live = BrainOutput.objects.filter(workspace_id__in=mine, state=BrainOutput.ACTIVE)
    about_me = Q(subject_type="person", subject=str(user.pk))
    q = (Q(capability__purpose_class=MEMORY) | to(INDIVIDUAL, "person")) & about_me
    coached = [str(i) for i in access.review_grantors(user).values_list("user_id", flat=True)]
    if coached:   # only where the coaching applies: the workspaces the coach covers, as their transcript reads
        q |= to(INDIVIDUAL, "coaches") & Q(subject_type="person", subject__in=coached,
                                           workspace_id__in=access.coach_scope(user))
    person = as_person and not participants.is_agent_login(user)
    if person:
        for ws, report in reporting.lines_of(user, mine):
            q |= to(INDIVIDUAL, "manager") & Q(subject_type="person", subject=str(report), workspace_id=ws)
    readable = (AgentProject.objects.filter(project_read_q(user)).annotate(key=Cast("pk", CharField()))
                .values("key"))
    q |= to(PROJECT, "project_readers") & Q(subject_type="project", subject__in=readable)
    if getattr(user, "is_superuser", False):
        q |= to(SYSTEM, "canopy_admins")
    # The owners' and admins' leg is the role alone (not `actor.may_manage`, whose superuser half is the
    # `canopy_admins` leg above), so a learner narrowed to `agent_admins` is not read by a superuser as such.
    if person and (managed := perms.slugs_with(user, perms.BRAIN_MANAGE)):
        q |= to(SYSTEM, "agent_admins") & Q(workspace_id__in=managed)
    elif (acting is not None and acting.is_agent and acting.user.pk == user.pk
          and (managed := acting.managed(mine))):
        q |= to(SYSTEM, "agent_admins") & Q(workspace_id__in=managed)
    return live.filter(q).distinct()


def _heard_by() -> dict[tuple[str, str], list[int]]:
    """(class, audience word) -> the learners of that class whose audience names it. A handful of rows, read once."""
    out: dict[tuple[str, str], list[int]] = {}
    for pk, cls, audience in BrainCapability.objects.values_list("pk", "purpose_class", "audience"):
        for word in audience if isinstance(audience, list) else []:
            out.setdefault((cls, word), []).append(pk)
    return out
