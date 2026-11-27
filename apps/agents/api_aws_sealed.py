"""Sealed AWS roles over REST: the role keys canopy-web carries as ciphertext, and an agent's
sealed credentials. The rules are `apps/agents/aws_sealed.py`'s module docstring.

* `/api/aws/sealed-keys…` — a canopy superuser registers a role's own AWS key from their
  browser, sealed there: to the approvers (people) or to one standing agent's machines.
  canopy-web keeps only ciphertext and the key's (public) id.
* `POST /api/agents/{slug}/aws-credentials/sealed` — the agent's machine asks for what it can
  open: its standing key for the role, or the session an approver's browser sealed to it for a
  live grant. `canopy aws credentials` opens it with the machine's key and, for a standing key,
  asks STS itself. 404 `no_sealed_source` sends it to the custodial `aws-credentials`.
"""
from __future__ import annotations

import datetime as dt
from typing import Literal

from django.contrib.auth import get_user_model
from django.http import HttpRequest
from ninja import Router, Schema
from ninja.errors import HttpError
from pydantic import Field

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.errors import ProblemError
from apps.api.route_gates import gate
from apps.passkeys import stepup

from . import aws_grants, aws_sealed
from .gates import agent_for_work
from .models import AwsSealedKey

router = Router(auth=session_auth, tags=["agents"])
keys_router = Router(auth=session_auth, tags=["aws"])


# --- the role keys (a superuser's browser) -----------------------------------------------------


class AwsSealedKeyOut(Schema):
    role: str
    #: The standing agent the key is sealed to the machines of; "" for the approvers' copy.
    agent: str = ""
    access_key_id: str
    #: Approvers' copy: who holds it (their emails). Standing: how many machines it is sealed to.
    holders: list[str] = []
    machines: int = 0
    created_by: str = ""
    updated_at: dt.datetime


class AwsSealedRoleOut(Schema):
    role: str
    role_arn: str
    summary: str = ""
    #: The agents standing for the role, each of which may have its own sealed key.
    standing: list[str] = []
    keys: list[AwsSealedKeyOut] = []


class AwsSealedHolderOut(Schema):
    user_id: str
    email: str
    public_jwk: dict
    jkt: str


class AwsSealedKeyIn(Schema):
    access_key_id: str = Field(pattern=r"^(AKIA|ASIA)[A-Z0-9]{12,28}$",
                               description="The key's id (not secret), so people can match it to its IAM user.")
    #: The key, `{"AccessKeyId", "SecretAccessKey"}` as JSON, sealed in your browser (`zks1`,
    #: binding `aws-role:<role>` or `aws-role:<role>@<agent>`, name `AWS_ACCESS_KEY`, kv 0).
    value_zk: str = Field(max_length=65536)
    #: A standing agent's machines; "" for the approvers.
    agent: str = ""


def _superuser(request: HttpRequest, what: str) -> None:
    user = request.user
    if not (getattr(user, "is_superuser", False) and user.is_active):
        raise HttpError(403, f"{what} is for a canopy superuser")


def _key_out(row: AwsSealedKey) -> AwsSealedKeyOut:
    users = get_user_model().objects.filter(pk__in=aws_sealed.holder_ids(row)) if row.agent_id is None else []
    machines = 0 if row.agent_id is None else len(aws_sealed.sealed_targets(row.value_zk))
    return AwsSealedKeyOut(role=row.role, agent=row.agent.slug if row.agent_id else "",
                           access_key_id=row.access_key_id, holders=sorted(u.email for u in users),
                           machines=machines, created_by=getattr(row.created_by, "email", "") or "",
                           updated_at=row.updated_at)


def _role(role: str) -> str:
    if role not in aws_grants.roles():
        raise HttpError(404, f"canopy does not vend an AWS role named '{role}'")
    return role


@keys_router.get("/sealed-keys", response=list[AwsSealedRoleOut], summary="Each AWS role and its sealed keys")
@gate("superuser")
def list_aws_sealed_keys(request: HttpRequest) -> list[AwsSealedRoleOut]:
    """Every role canopy vends, the agents standing for it, and the sealed keys registered for it
    (ids and who holds them; never a value canopy-web could use)."""
    _superuser(request, "Sealed AWS keys")
    rows = list(AwsSealedKey.objects.select_related("agent", "created_by"))
    return [AwsSealedRoleOut(role=role, role_arn=arn, summary=aws_grants.summary(role),
                             standing=aws_grants.standing(role) or [],
                             keys=[_key_out(r) for r in rows if r.role == role])
            for role, arn in sorted(aws_grants.roles().items())]


@keys_router.get("/sealed-keys/holders", response=list[AwsSealedHolderOut],
                 summary="Who a role's approver key may be sealed to (public keys)")
@gate("superuser")
def list_aws_sealed_key_holders(request: HttpRequest) -> list[AwsSealedHolderOut]:
    """canopy superusers with a vault key, each with its public key. This list is canopy-web's
    claim: check each key against one you trust before sealing to it."""
    _superuser(request, "Sealed AWS keys")
    return [AwsSealedHolderOut(**h) for h in aws_sealed.holder_candidates()]


@keys_router.get("/sealed-keys/machines/{slug}", response=dict,
                 summary="The machines that run a standing agent, with who vouched for each key")
@gate("superuser")
def list_aws_sealed_key_machines(request: HttpRequest, slug: str) -> dict:
    """The agent's device keys and their endorsements (`ZkRecipientsOut`): seal only to keys
    endorsed by someone you trust."""
    _superuser(request, "Sealed AWS keys")
    agent = Agent.objects.filter(slug=slug).first()
    if agent is None:
        raise HttpError(404, f"no agent {slug}")
    return aws_sealed.agent_devices(agent)


@keys_router.put("/sealed-keys/{role}", response=AwsSealedKeyOut,
                 summary="Register a role's own AWS key, sealed in your browser")
@gate("superuser", setting=True, step_up=True)
def put_aws_sealed_key(request: HttpRequest, role: str, payload: AwsSealedKeyIn) -> AwsSealedKeyOut:
    """Store the role's key as ciphertext: sealed to the approvers (superusers with a vault key),
    or to the machines that run an agent standing for the role. A browser only, after a passkey
    step-up; canopy-web checks who it is sealed to and never opens it. Replaces the one before."""
    _superuser(request, "Registering a sealed AWS key")
    stepup.require_browser(request, "registering a sealed AWS key")
    stepup.require_recent_step_up(request)
    _role(role)
    agent = None
    if payload.agent:
        agent = Agent.objects.filter(slug=payload.agent).first()
        if agent is None:
            raise HttpError(404, f"no agent {payload.agent}")
        if agent.slug not in (aws_grants.standing(role) or []):
            raise HttpError(422, f"{agent.slug} does not stand for {role} (settings.CANOPY_AWS_ROLE_AGENTS): "
                                 "its access comes from a grant a person approves, not a key of its own")
    try:
        if agent is None:
            aws_sealed.check_holders(payload.value_zk)
        else:
            aws_sealed.check_devices(payload.value_zk, agent, aws_sealed.KEY_NAME)
    except aws_sealed.SealedError as exc:
        raise HttpError(422, str(exc)) from exc
    row, _ = AwsSealedKey.objects.update_or_create(
        role=role, agent=agent,
        defaults={"access_key_id": payload.access_key_id, "value_zk": payload.value_zk, "created_by": request.user})
    return _key_out(row)


@keys_router.delete("/sealed-keys/{role}", response=dict, summary="Forget a role's sealed key")
@gate("superuser", setting=True)
def delete_aws_sealed_key(request: HttpRequest, role: str, agent: str = "") -> dict:
    """Delete the approvers' copy (or `agent`'s). Deactivate the key in IAM as well: its holders
    could have kept a copy."""
    _superuser(request, "Deleting a sealed AWS key")
    n, _ = AwsSealedKey.objects.filter(role=_role(role), **({"agent__slug": agent} if agent else
                                                            {"agent__isnull": True})).delete()
    return {"deleted": n}


# --- the agent's machine ------------------------------------------------------------------------


class AwsSealedCredentialsIn(Schema):
    role: str


class AwsSealedCredentialsOut(Schema):
    #: `key`: the agent's standing key for the role; open it and call STS with `sts`.
    #: `session`: a session an approver's browser made and sealed to this machine.
    kind: Literal["key", "session"]
    value_zk: str
    binding: str
    name: str
    kv: int = 0
    #: For `key`: what to ask STS (`AssumeRole`), stamped with the agent.
    sts: dict | None = None
    #: For `session`: when its keys end.
    expires_at: dt.datetime | None = None
    grant: int | None = None


@router.post("/{slug}/aws-credentials/sealed", response=AwsSealedCredentialsOut,
             summary="This agent's sealed AWS credentials for a role, for its machine to open")
# the agent's own login, or agent.work — with a token; ciphertext only this agent's machines open
@gate("agent.work", "agent-login", kind="ephemeral-credential",
      step_up="ciphertext sealed to the agent's machines: nothing a browser could use")
def agent_aws_sealed_credentials(request: HttpRequest, slug: str, payload: AwsSealedCredentialsIn):
    """What this agent's machine can open for `role`: its standing key (`kind: key`, with what to
    ask STS), or the session an approver sealed to it for its live sealed grant (`kind: session`).
    404 `no_sealed_source` when neither exists (use `POST aws-credentials`); 409 `session_pending`
    while a sealed grant waits for its approver's browser to hand the session over."""
    agent = agent_for_work(request, slug, allow_own_login=True,
                           forbidden=f"AWS credentials for {slug} need the editor role or above, "
                                     "or the agent's own login")
    if stepup.is_browser(request):
        raise ProblemError(403, "A token is required", detail="sealed AWS credentials go to the agent's machine")
    role = _role(payload.role)
    standing = aws_sealed.standing_key(role, agent)
    if standing is not None and agent.slug in (aws_grants.standing(role) or []):
        caller = "agent-login" if agent.is_own_login(request.user) else f"user-{request.user.pk}"
        return AwsSealedCredentialsOut(
            kind="key", value_zk=standing.value_zk, binding=aws_sealed.key_binding(role, agent),
            name=aws_sealed.KEY_NAME, sts=aws_sealed.sts_request(agent, role, caller=caller, seconds=3600))
    grant = aws_sealed.live_sealed_grant(agent, role)
    if grant is not None:
        if not aws_sealed.session_ok(grant):
            raise ProblemError(409, "Waiting for the approver's browser",
                               detail=f"grant {grant.pk} is approved, but its approver's browser has not handed "
                                      "over a live session yet; it does as they approve (or from the request's "
                                      "page, 'Make new keys')",
                               members={"error": "session_pending", "grant": grant.pk})
        return AwsSealedCredentialsOut(kind="session", value_zk=grant.sealed_session,
                                       binding=aws_sealed.session_binding(grant), name=aws_sealed.SESSION_NAME,
                                       expires_at=grant.sealed_session_expires_at, grant=grant.pk)
    raise ProblemError(404, "No sealed source", detail=f"{agent.slug} has no sealed key or sealed grant for {role}",
                       members={"error": "no_sealed_source"})
