"""`POST /api/agents/{slug}/aws-credentials` — one hour of a NAMED, read-only AWS role, as the agent.

Why. An agent turn that needs labs telemetry (CloudWatch metrics, alarm history, ECS
service state) used to need the `labs` SSO profile: AdministratorAccess, renewed by a
person approving a device code on their phone (`canopy aws login`). A turn on a laptop
or a cloud runner can now ask canopy-web instead, for a role that can read only
NON-SENSITIVE things — `labs-observe` is `labs-jj-observe-readonly`
(connect-labs `infra/labs-observe-readonly.yml`): metrics, alarms and resource
metadata, with explicit denies on log events, object bodies, secrets, task commands and
environments, and database content.

How. canopy-web's own task role (`canopy-web-task-role`) calls `sts:AssumeRole` on the
role, stamping the agent onto the session — `SourceIdentity=canopy-agent-<slug>` (sticky,
so CloudTrail names the agent on every call made with these keys) and session tags
`agent`, `caller`. The response is exactly the AWS CLI's `credential_process` shape, so
`canopy aws credentials --role labs-observe` prints it unchanged.

Which roles. Only the names in `settings.CANOPY_AWS_ROLES` (name -> role ARN). A name
not there is a 404: this is not a general assume-role service, and adding a role is a
settings change a person reviews. The role's own trust policy must also name
canopy-web-task-role, so canopy-web cannot vend a role its owner did not agree to.

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
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import ProblemError
from apps.api.route_gates import gate
from apps.events import services as events
from apps.passkeys import stepup

from .gates import agent_for_work

logger = logging.getLogger(__name__)

router = Router(auth=session_auth, tags=["agents"])

#: STS's own limit for a role-chained session, and the role's MaxSessionDuration.
SESSION_SECONDS = 3600
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
             summary="One hour of a named read-only AWS role, as this agent")
# the agent's own login, or agent.work — with a token; a browser session is refused
@gate("agent.work", "agent-login", kind="ephemeral-credential",
      step_up="token callers only: a browser session is refused, the keys go to a CLI's cache")
def agent_aws_credentials(request: HttpRequest, slug: str, payload: AwsCredentialsIn) -> AwsCredentialsOut:
    """Short-lived AWS keys for ONE role named in canopy-web's settings
    (`labs-observe` = the read-only, non-sensitive labs observability role), with the
    agent stamped on the session (`SourceIdentity=canopy-agent-<slug>`). 404 for a role
    name canopy-web does not vend; 403 for a browser session; 502 when AWS refused."""
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
    caller = "agent-login" if agent.is_own_login(request.user) else f"user-{request.user.pk}"
    try:
        out = sts_client().assume_role(
            RoleArn=role_arn,
            RoleSessionName=_sts_name(f"canopy-{agent.slug}"),
            DurationSeconds=SESSION_SECONDS,
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
                "summary": f"{agent.slug}: one hour of AWS role {payload.role}",
                "payload": {"agent": agent.slug, "role": payload.role, "role_arn": role_arn,
                            "caller": caller, "expires": str(expiration)},
            }],
            workspace=agent.workspace,
        )
    except Exception:  # noqa: BLE001 — an audit hiccup must not deny the read-only keys
        pass
    return AwsCredentialsOut(AccessKeyId=creds["AccessKeyId"], SecretAccessKey=creds["SecretAccessKey"],
                             SessionToken=creds["SessionToken"], Expiration=str(expiration),
                             RoleArn=role_arn)
