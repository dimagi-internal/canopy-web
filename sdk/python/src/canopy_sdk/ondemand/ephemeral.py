"""One EC2 instance per lease, launched from a capability's launch template and terminated after.

The ephemeral counterpart of ``OnDemandInstance``: where that one starts and stops
a single long-lived box, this launches a FRESH instance per lease from the
capability stack's launch template (``ondemand-capability.cfn.yaml`` with
``Mode: ephemeral``) and terminates it when the lease ends. A fresh machine from
the image is the precondition, so nothing is ever inherited from the last user.

Every method is one AWS call (or one paginated describe) and returns at once:
there is no waiting and no polling here. The lease's state lives in EC2, SSM and
S3, so a caller that restarts mid-lease loses nothing — it reads it back.

- ``launch`` is idempotent: ``ClientToken=lease_id``, so a retry after a crash or
  a timeout returns the SAME instance instead of starting a second one.
- ``list_tagged`` is how a caller counts live machines and finds orphans: every
  instance carries ``canopy:capability`` and ``canopy:owner`` (from the launch
  template and from the request) and ``canopy:lease``; canopy-web's broker also
  tags ``canopy:broker`` (``TAG_BROKER``) with its deployment's id.
- ``run_document_async`` sends the capability stack's own exec document, which
  runs an argv only if argv[0] is on the stack's allow list.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .errors import InstanceGone, OnDemandError, SSMFailure
from .ssm import send_command

TAG_CAPABILITY = "canopy:capability"
TAG_OWNER = "canopy:owner"
TAG_LEASE = "canopy:lease"
#: Which broker deployment launched the instance (a canopy-web's own id), so two
#: brokers sharing one capability stack never count or kill each other's machines.
TAG_BROKER = "canopy:broker"

#: EC2 states of an instance that still costs money or is about to. Not
#: ``shutting-down`` / ``terminated``: those are already on their way out.
LIVE_STATES = ("pending", "running", "stopping", "stopped")

#: SSM invocation statuses after which nothing more will happen.
TERMINAL_INVOCATION_STATUSES = frozenset({"Success", "Cancelled", "TimedOut", "Failed"})

#: EC2 error codes that mean "try the same launch again later" — the lease stays
#: where it is, and the retry is safe because of the ClientToken.
TRANSIENT_LAUNCH_ERRORS = frozenset({
    "RequestLimitExceeded", "Throttling", "ThrottlingException", "InsufficientInstanceCapacity",
    "InternalError", "ServiceUnavailable", "Unavailable",
})


class LaunchFailed(OnDemandError):
    """``RunInstances`` refused the launch. ``transient`` says whether retrying can help."""

    def __init__(self, message: str, *, code: str = "", transient: bool = False):
        super().__init__(message)
        self.code = code
        self.transient = transient


@dataclass
class Described:
    instance_id: str
    state: str
    launch_time: dt.datetime | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def lease_id(self) -> str:
        return self.tags.get(TAG_LEASE, "")


@dataclass
class Invocation:
    """One SSM command's state, read back from SSM (never from memory)."""

    command_id: str
    instance_id: str
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_url: str = ""
    stderr_url: str = ""

    @property
    def done(self) -> bool:
        return self.status in TERMINAL_INVOCATION_STATUSES


class EphemeralInstance:
    """Launch, inspect, command and terminate one capability's per-lease instances.

    ``capability`` and ``owner`` are the values the stack tags instances with
    (``canopy:capability`` / ``canopy:owner``); the stack's broker policy only
    lets a caller launch with those request tags and terminate or command
    instances that carry them.
    """

    def __init__(self, *, region: str, launch_template_id: str, capability: str, owner: str,
                 ec2: Any = None, ssm: Any = None):
        if not launch_template_id:
            raise ValueError("launch_template_id is required")
        if not capability or not owner:
            raise ValueError("capability and owner are required")
        self.region = region
        self.launch_template_id = launch_template_id
        self.capability = capability
        self.owner = owner
        self.ec2 = ec2 or boto3.client("ec2", region_name=region)
        self.ssm = ssm or boto3.client("ssm", region_name=region)

    # ---- lifecycle -------------------------------------------------------

    def tags_for(self, lease_id: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {TAG_CAPABILITY: self.capability, TAG_OWNER: self.owner, TAG_LEASE: lease_id,
                **(extra or {})}

    def launch(self, *, lease_id: str, user_data: dict | None = None,
               tags: dict[str, str] | None = None, version: str = "$Default") -> str:
        """``RunInstances`` from the launch template; returns the instance id.

        ``ClientToken=lease_id`` makes this idempotent: calling it again with the
        same lease (after a crash, a timeout, a retry) returns the instance the
        first call started. ``user_data`` is serialised as JSON — the image reads
        it from IMDS. Raises ``LaunchFailed`` (``.transient`` when a retry may
        succeed)."""
        if not lease_id or len(lease_id) > 64:
            raise ValueError("lease_id must be 1-64 characters (it is the EC2 ClientToken)")
        all_tags = self.tags_for(lease_id, tags)
        tag_list = [{"Key": k, "Value": str(v)} for k, v in all_tags.items()]
        kwargs: dict[str, Any] = {
            "LaunchTemplate": {"LaunchTemplateId": self.launch_template_id, "Version": version},
            "MinCount": 1,
            "MaxCount": 1,
            "ClientToken": lease_id,
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": tag_list},
                {"ResourceType": "volume", "Tags": tag_list},
            ],
        }
        if user_data is not None:
            raw = json.dumps(user_data, separators=(",", ":"), sort_keys=True).encode()
            kwargs["UserData"] = base64.b64encode(raw).decode()
        try:
            resp = self.ec2.run_instances(**kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            raise LaunchFailed(f"run_instances failed for lease {lease_id}: {e}", code=code,
                               transient=code in TRANSIENT_LAUNCH_ERRORS) from e
        instances = resp.get("Instances") or []
        if not instances or not instances[0].get("InstanceId"):
            raise LaunchFailed(f"run_instances returned no instance for lease {lease_id}: {resp!r}")
        return instances[0]["InstanceId"]

    def describe(self, instance_id: str) -> Described:
        """One instance's state and tags. Raises ``InstanceGone`` if EC2 no longer knows it."""
        try:
            r = self.ec2.describe_instances(InstanceIds=[instance_id])
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"):
                raise InstanceGone(f"{self.capability} instance {instance_id} does not exist") from e
            raise OnDemandError(f"describe_instances failed for {instance_id}: {e}") from e
        insts = [i for res in r.get("Reservations", []) for i in res.get("Instances", [])]
        if not insts:
            raise InstanceGone(f"{self.capability} instance {instance_id} does not exist")
        return _described(insts[0])

    def terminate(self, *instance_ids: str) -> None:
        """``TerminateInstances``. An id EC2 no longer knows is not an error."""
        ids = [i for i in instance_ids if i]
        if not ids:
            return
        try:
            self.ec2.terminate_instances(InstanceIds=ids)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "InvalidInstanceID.NotFound":
                return
            raise OnDemandError(f"terminate_instances failed for {ids}: {e}") from e

    def list_tagged(self, states: tuple[str, ...] = LIVE_STATES) -> list[Described]:
        """Every instance tagged with this capability and owner, in ``states``."""
        filters = [
            {"Name": f"tag:{TAG_CAPABILITY}", "Values": [self.capability]},
            {"Name": f"tag:{TAG_OWNER}", "Values": [self.owner]},
            {"Name": "instance-state-name", "Values": list(states)},
        ]
        out: list[Described] = []
        token = None
        try:
            while True:
                kwargs: dict[str, Any] = {"Filters": filters}
                if token:
                    kwargs["NextToken"] = token
                r = self.ec2.describe_instances(**kwargs)
                for res in r.get("Reservations", []):
                    out.extend(_described(i) for i in res.get("Instances", []))
                token = r.get("NextToken")
                if not token:
                    return out
        except ClientError as e:
            raise OnDemandError(f"describe_instances (tagged {self.capability}) failed: {e}") from e

    # ---- commands --------------------------------------------------------

    def run_async(self, instance_id: str, commands: list[str], *, timeout_s: int = 600,
                  output_bucket: str | None = None, output_prefix: str | None = None,
                  comment: str | None = None) -> str:
        """Send a shell script and return its SSM CommandId without waiting.

        SSM is the job store: read the result back later with ``invocation`` from
        any process. With ``output_bucket`` the full streams land in S3 under
        ``output_prefix``."""
        return send_command(
            self.ssm, instance_id, commands=commands, timeout_seconds=timeout_s,
            execution_timeout_s=timeout_s, output_bucket=output_bucket,
            output_prefix=output_prefix, comment=comment,
        )

    def run_document_async(self, instance_id: str, document: str, parameters: dict[str, list[str]], *,
                           timeout_s: int = 600, output_bucket: str | None = None,
                           output_prefix: str | None = None, comment: str | None = None,
                           document_version: str | None = None) -> str:
        """Send a named command document (e.g. the capability stack's exec
        document) with ``parameters`` and return its SSM CommandId at once.
        ``timeout_s`` is SSM's delivery timeout; the document bounds the run."""
        return send_command(
            self.ssm, instance_id, commands=[], timeout_seconds=timeout_s,
            output_bucket=output_bucket, output_prefix=output_prefix, comment=comment,
            document_name=document, parameters=parameters, document_version=document_version,
        )

    def invocation(self, command_id: str, instance_id: str) -> Invocation | None:
        """The command's current state on ``instance_id``, or None if SSM has no
        such invocation (not materialised yet, or not this instance's)."""
        try:
            r = self.ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("InvocationDoesNotExist", "InvalidCommandId", "InvalidInstanceId"):
                return None
            raise SSMFailure(f"SSM get_command_invocation failed: {e}") from e
        status = r.get("Status", "Pending")
        raw = r.get("ResponseCode")
        exit_code = int(raw) if raw is not None and status in TERMINAL_INVOCATION_STATUSES else None
        if exit_code is not None and exit_code < 0 and status == "Success":
            exit_code = 0
        return Invocation(
            command_id=command_id, instance_id=instance_id, status=status, exit_code=exit_code,
            stdout=r.get("StandardOutputContent", "") or "",
            stderr=r.get("StandardErrorContent", "") or "",
            stdout_url=r.get("StandardOutputUrl", "") or "",
            stderr_url=r.get("StandardErrorUrl", "") or "",
        )


def _described(inst: dict) -> Described:
    return Described(
        instance_id=inst["InstanceId"],
        state=(inst.get("State") or {}).get("Name", ""),
        launch_time=inst.get("LaunchTime"),
        tags={t["Key"]: t["Value"] for t in inst.get("Tags") or []},
    )
