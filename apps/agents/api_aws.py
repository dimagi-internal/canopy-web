"""`POST /api/agents/{slug}/aws-credentials` — at most an hour of a NAMED AWS role, as the agent.

Why. An agent turn that needs labs telemetry (CloudWatch metrics, alarm history, ECS
service state) would otherwise need the `labs` SSO profile: AdministratorAccess, renewed by a
person approving a device code on their phone (`canopy aws login`). A turn on a laptop
or a cloud runner asks canopy-web instead, for a role that can do one bounded thing. What
each role can do is one line in `settings.CANOPY_AWS_ROLE_SUMMARIES`, the line an approver
reads: `labs-read` (alias `labs-observe`) is `labs-jj-observe-readonly` (connect-labs
`infra/labs-observe-readonly.yml`), NON-SENSITIVE reads only — metrics, alarms, resource and
RDS metadata, with explicit denies on log events, object bodies, secrets, task commands and
environments, and database content; `labs-readwrite` is `labs-jj-agent-readwrite`
(`infra/labs-agent-readwrite.yml`): agent-named RDS copies of production and one psql client
task, production source-only, IAM changes denied; `labs-config` is `labs-jj-agent-config`
(`infra/labs-agent-config.yml`): create and update the labs app secrets named `labs-jj-app-*`,
never read a value, no deletes, no IAM.

How. canopy-web's own task role (`canopy-web-task-role`) calls `sts:AssumeRole` on the
role, stamping the agent onto the session — `SourceIdentity=canopy-agent-<slug>` (sticky,
so CloudTrail names the agent on every call made with these keys) and session tags
`agent`, `caller`. The response is exactly the AWS CLI's `credential_process` shape, so
`canopy aws credentials --role labs-read` prints it unchanged.

Which roles. Only the names in `settings.CANOPY_AWS_ROLES` (name -> role ARN). A name
not there is a 404: this is not a general assume-role service, and adding a role is a
settings change a person reviews. The role's own trust policy must also name
canopy-web-task-role, so canopy-web cannot vend a role its owner did not agree to.

Fenced roles are requested when needed and approved by a person. A role named in
`settings.CANOPY_AWS_ROLE_AGENTS` is fenced (`labs-readwrite`, `labs-config`). The agents
listed beside it would get it with no approval, and every list is empty, so no agent holds a
fenced role standing: an agent gets one only while it holds a LIVE GRANT (`AgentAwsRoleRequest`,
`apps/agents/aws_grants.py`). `labs-read` is not fenced and goes to any agent on ask:

1. The agent (its own login or turn, or a member who may work it) asks:
   `POST /api/agents/{slug}/aws-role-requests {role, reason, minutes}` (15 to 480 minutes,
   default 60; `apps/approvals/api_aws_roles.py`, `canopy aws request`). A role vended on ask
   (`labs-read`) is refused there, since it needs no approval.
2. That files an approval of kind `agent.aws_role` (`apps/approvals/kinds/agent_aws_role.py`,
   `step_up`): its preview names the agent, the role and its ARN, the role's summary line and
   the duration, beside the reason as the requester's own words. It waits 24 hours.
3. An OWNER of the agent's workspace or a canopy SUPERUSER approves it on the approval's page,
   with a passkey. Never an agent — not the one asking, not another — and an agent's own admins
   are not enough: these are AWS account rights. Approving makes the grant, which ends `minutes`
   after the approval (`expires_at`).
4. Here, a fenced role is vended to a non-listed agent only under a live grant, and the STS
   session lasts the grant's remaining time, at most an hour (STS's limit for a chained role)
   and never past the grant. Under 15 minutes left (STS's shortest session) counts as no grant.
   Without one the answer is 403 with
   `{"error": "approval_required", "request_with": "POST /api/agents/<slug>/aws-role-requests"}`.
5. `DELETE /api/agents/{slug}/aws-role-requests/{id}` revokes a grant at once — by the person
   who approved it, a workspace owner or a superuser — or withdraws a pending request (also by
   whoever asked). Keys already handed out last until their own expiry.

canopy-web is the ONLY source of a fenced role; a cloud runner's box broker never vends one,
because it cannot verify which agent is asking.

Who. The agent's own canopy login (what a turn and a laptop agent session hold), or a
member who may work the agent (`agent.work`), with a TOKEN: a browser session is refused,
because these keys belong in a CLI's credential cache, never on a page. Never an MCP
tool (`apps/mcp/api_tools.EXCLUDED`): a tool result would put the keys in a model's
context.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from django.conf import settings
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import ProblemError
from apps.api.route_gates import gate
from apps.events import services as events
from apps.passkeys import stepup

from . import aws_grants
from .gates import agent_for_work

logger = logging.getLogger(__name__)

router = Router(auth=session_auth, tags=["agents"])

#: STS's own limit for a role-chained session, and the role's MaxSessionDuration.
SESSION_SECONDS = 3600
#: STS's shortest session: a grant with less left than this vends nothing.
MIN_SESSION_SECONDS = 900
_STS_NAME = re.compile(r"[^\w+=,.@-]")


class AwsCredentialsIn(Schema):
    role: str


class AwsCredentialsOut(Schema):
    """The AWS CLI's `credential_process` output, version 1."""

    Version: int = 1
    AccessKeyId: str
    SecretAccessKey: str
    SessionToken: str
    Expiration: str
    #: Not part of the credential_process contract (the CLI ignores extra keys);
    #: says which role these keys are, for a person reading `canopy aws credentials`.
    RoleArn: str


def sts_client():
    """The one seam tests replace. canopy-web's task role is the caller."""
    import boto3  # noqa: PLC0415 — boto3 is heavy; tests replace this seam and never import it

    return boto3.client("sts", region_name=getattr(settings, "CANOPY_AWS_STS_REGION", "us-east-1"))


def _sts_name(text: str) -> str:
    """A value STS accepts as a session name / source identity (2-64 of [\\w+=,.@-])."""
    out = _STS_NAME.sub("-", text)[:64]
    return out if len(out) >= 2 else (out + "--")[:2]


@router.post("/{slug}/aws-credentials", response=AwsCredentialsOut,
             summary="At most an hour of a named AWS role, as this agent")
# the agent's own login, or agent.work — with a token; a browser session is refused
@gate("agent.work", "agent-login", kind="ephemeral-credential",
      step_up="token callers only: a browser session is refused, the keys go to a CLI's cache")
def agent_aws_credentials(request: HttpRequest, slug: str, payload: AwsCredentialsIn) -> AwsCredentialsOut:
    """Short-lived AWS keys for ONE role named in canopy-web's settings (`labs-read` = read-only,
    non-sensitive labs observability, on ask; `labs-readwrite` and `labs-config` are fenced), with
    the agent stamped on the session (`SourceIdentity=canopy-agent-<slug>`). A fenced role goes to
    the agents listed for it, and to another agent only while it holds a live grant a person
    approved (`POST /api/agents/{slug}/aws-role-requests`), for at most the grant's remaining time.
    404 for a role name canopy-web does not vend; 403 for a browser session, or `approval_required`
    (with `request_with`) for a fenced role and no live grant; 502 when AWS refused."""
    agent = agent_for_work(request, slug, allow_own_login=True,
                           forbidden=f"AWS credentials for {slug} need the editor role or above, "
                                     "or the agent's own login")
    if stepup.is_browser(request):
        raise ProblemError(403, "A token is required",
                           detail="AWS keys are only handed to a CLI (canopy aws credentials), "
                                  "never to a browser session")
    roles = dict(getattr(settings, "CANOPY_AWS_ROLES", {}) or {})
    role_arn = roles.get(payload.role)
    if not role_arn:
        raise HttpError(404, f"canopy does not vend an AWS role named '{payload.role}'"
                             + (f" (it vends: {', '.join(sorted(roles))})" if roles else ""))
    duration, grant = SESSION_SECONDS, None
    only = aws_grants.standing(payload.role)
    if only is not None and agent.slug not in only:
        grant = aws_grants.live_grant(agent, payload.role)
        left = int((grant.expires_at - timezone.now()).total_seconds()) if grant else 0
        if left < MIN_SESSION_SECONDS:
            request_with = f"POST /api/agents/{agent.slug}/aws-role-requests"
            raise ProblemError(
                403, "This AWS role needs a person's approval",
                detail=(f"{agent.slug} holds no live grant for {payload.role}"
                        + (" (the grant ends in under 15 minutes)" if grant else "")
                        + f". Ask with {request_with} {{role, reason, minutes}} (canopy aws request "
                        f"--role {payload.role} --reason ...); an owner of {agent.workspace_id} approves it "
                        "with a passkey."),
                members={"error": "approval_required", "request_with": request_with})
        duration = min(SESSION_SECONDS, left)
    caller = "agent-login" if agent.is_own_login(request.user) else f"user-{request.user.pk}"
    try:
        out = sts_client().assume_role(
            RoleArn=role_arn,
            RoleSessionName=_sts_name(f"canopy-{agent.slug}"),
            DurationSeconds=duration,
            SourceIdentity=_sts_name(f"canopy-agent-{agent.slug}"),
            Tags=[{"Key": "agent", "Value": agent.slug}, {"Key": "caller", "Value": caller}],
        )
    except Exception as exc:  # noqa: BLE001 — botocore's errors; the message is safe to show
        logger.warning("aws-credentials: assume_role %s for %s failed: %s", role_arn, agent.slug, exc)
        raise ProblemError(502, "AWS refused the role",
                           detail=f"sts:AssumeRole on {role_arn} failed: {str(exc)[:300]}") from exc
    creds = out["Credentials"]
    expiration = creds["Expiration"]
    if isinstance(expiration, dt.datetime):
        expiration = expiration.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        events.record(
            [{
                "source": "agents.aws",
                "kind": "agent.aws.issued",
                "level": "info",
                "key": f"{agent.slug}:{payload.role}",
                "summary": f"{agent.slug}: {duration // 60} minutes of AWS role {payload.role}"
                           + (f" (grant {grant.pk})" if grant else ""),
                "payload": {"agent": agent.slug, "role": payload.role, "role_arn": role_arn,
                            "caller": caller, "expires": str(expiration), "seconds": duration,
                            "grant": grant.pk if grant else None},
            }],
            workspace=agent.workspace,
        )
    except Exception:  # noqa: BLE001 — an audit hiccup must not deny the keys
        pass
    return AwsCredentialsOut(AccessKeyId=creds["AccessKeyId"], SecretAccessKey=creds["SecretAccessKey"],
                             SessionToken=creds["SessionToken"], Expiration=str(expiration),
                             RoleArn=role_arn)
