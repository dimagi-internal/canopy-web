"""The kinds of thing a person can be asked to approve — a closed, hard-coded set.

A kind is Python, not prose: typed `params`, a `describe()` that renders what approving will do
from those same params, a `may_approve()` that says who holds the authority, and an `execute()`
that does it AS the approver. Nothing about what runs comes from the requester's wording, and no
model is involved in approving. A new kind is a new module here, registered with `@register`.

    @register
    class WorkspaceMemberAdd(Kind):
        slug = "workspace.member.add"
        label = "Add someone to a workspace"
        params_model = Params
        needs = "an admin or owner of the workspace"
        def describe(self, p): ...
        def may_approve(self, user, p, d): ...
        def execute(self, user, p, request): ...
"""
from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from pydantic import BaseModel, ValidationError

from apps.agents.models import Agent
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership


class KindError(ValueError):
    """The request cannot be filed: an unknown kind, bad params, or something it names is not there."""


class ExecError(Exception):
    """Approving was refused by the system itself (the person is already there, the target is gone,
    the approver's authority does not cover it). The message is shown to the approver."""


@dataclass(frozen=True)
class Described:
    """What approving will do, as the server renders it from the params."""

    workspace: str
    title: str
    summary: str
    plan: list[str]
    scopes: list[str]
    #: The person the action is about. They never approve their own request.
    about_email: str = ""


class Kind:
    """Subclass, set the class attributes, implement the three methods, and `@register`."""

    slug: str = ""
    label: str = ""
    params_model: type[BaseModel] = BaseModel
    #: In words: who can approve this ("an admin or owner of the workspace").
    needs: str = ""
    #: A line for the catalog: what the handler does.
    does: str = ""

    def describe(self, p) -> Described:  # pragma: no cover — abstract
        raise NotImplementedError

    def may_approve(self, user, p, d: Described) -> bool:  # pragma: no cover — abstract
        raise NotImplementedError

    def execute(self, user, p, request) -> dict:  # pragma: no cover — abstract
        raise NotImplementedError

    def recipients(self, p, d: Described) -> list:
        """Who is TOLD: the people accountable for this, not everyone who could approve it.

        By default the workspace's own (direct) members who hold the authority — Connect's admins, not
        the parent organisation's owners reaching it by inheritance — and only if there are none, all
        who could approve. A kind with a better answer (an agent's owner) overrides this."""
        approvers = self.approvers(p, d)
        direct = set(WorkspaceMembership.objects.filter(
            workspace_id=d.workspace, user__in=approvers).values_list("user_id", flat=True))
        return [u for u in approvers if u.pk in direct] or approvers

    def approvers(self, p, d: Described) -> list:
        """The people who could approve it, strongest authority first."""
        ws = Workspace.objects.filter(slug=d.workspace).first()
        if ws is None:
            return []
        bots = set(Agent.objects.filter(user__isnull=False).values_list("user_id", flat=True))
        rows = [m for m in wsvc.effective_memberships(ws) if m.user_id not in bots and m.user.is_active]
        out = []
        for m in rows:
            if d.about_email and (m.user.email or "").lower() == d.about_email.lower():
                continue
            if self.may_approve(m.user, p, d):
                out.append(m.user)
        return sorted(out, key=lambda u: u.pk)


REGISTRY: dict[str, Kind] = {}


def register(cls):
    kind = cls()
    if not kind.slug or kind.slug in REGISTRY:
        raise RuntimeError(f"approval kind {kind.slug!r} is empty or registered twice")
    REGISTRY[kind.slug] = kind
    return cls


def get(key: str) -> Kind:
    _load()
    kind = REGISTRY.get(key)
    if kind is None:
        raise KindError(f"unknown kind {key!r}; the kinds are: {', '.join(sorted(REGISTRY))}")
    return kind


def parse(key: str, params: dict):
    """(kind, validated params) — or KindError saying what is wrong, in words an agent can fix."""
    kind = get(key)
    try:
        return kind, kind.params_model.model_validate(params or {})
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(x) for x in e['loc']) or 'params'}: {e['msg']}"
                             for e in exc.errors())
        raise KindError(f"bad params for {key}: {problems}") from None


def catalog() -> list[dict]:
    _load()
    return [{"key": k.slug, "label": k.label, "needs": k.needs, "does": k.does,
             "params_schema": k.params_model.model_json_schema()}
            for k in sorted(REGISTRY.values(), key=lambda k: k.slug)]


def user_by_email(email: str):
    return get_user_model().objects.filter(email__iexact=(email or "").strip(), is_active=True).first()


_loaded = False


def _load() -> None:
    """Import every kind module once (they register themselves)."""
    global _loaded
    if _loaded:
        return
    from . import agent_admin, agent_turns, workspace_members  # noqa: F401, PLC0415 — registers the kinds

    _loaded = True
