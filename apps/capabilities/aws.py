"""Every AWS call the capability broker makes, behind one injectable seam.

`get(region)` returns the broker for a region. Tests replace it
(`monkeypatch.setattr(aws, "get", lambda region: fake)`), so no test reaches AWS
and no other module imports boto3 for capabilities.

canopy-web holds no AWS permission of its own for this: every call here is one
the agent's capability stack granted its task role (`BrokerPolicy` in
canopy_sdk's ondemand-capability template), scoped to that agent's tagged
instances, bucket and stack. EC2 and SSM go through
`canopy_sdk.ondemand.EphemeralInstance`, the same lifecycle any other consumer
of the template uses.
"""
from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from canopy_sdk.ondemand import Described, EphemeralInstance, Invocation, OnDemandError

logger = logging.getLogger(__name__)

#: The stack Outputs an ec2-ephemeral capability must expose (the contract).
REQUIRED_OUTPUTS = ("LaunchTemplateId", "ArtifactsBucketName", "InstanceRoleArn", "Capability")
#: Outputs canopy-web also reads: who the stack is for, and the role id that
#: names each instance's S3 prefix (vm/<InstanceRoleId>:<instance-id>/).
CHECKED_OUTPUTS = ("Owner", "InstanceRoleId")


class AwsError(Exception):
    """An AWS call failed. The message is safe to show the caller."""


@dataclass(frozen=True)
class Target:
    """One published capability, as the broker needs it."""

    region: str
    capability: str
    owner: str
    launch_template_id: str
    bucket: str
    role_id: str

    def ready_key(self, instance_id: str) -> str:
        # An instance-profile session's aws:userid is "<role id>:<instance id>",
        # and the instance role may write only vm/${aws:userid}/*.
        return f"vm/{self.role_id}:{instance_id}/ready.json"


class Broker(Protocol):
    def stack_outputs(self, stack: str) -> dict[str, str]: ...
    def launch(self, t: Target, *, lease_id: str, user_data: dict) -> str: ...
    def describe(self, t: Target, instance_id: str) -> Described | None: ...
    def terminate(self, t: Target, instance_ids: list[str]) -> None: ...
    def list_tagged(self, t: Target) -> list[Described]: ...
    def read_json(self, bucket: str, key: str) -> dict | None: ...
    def run_async(self, t: Target, instance_id: str, commands: list[str], *, timeout_s: int,
                  output_prefix: str) -> str: ...
    def invocation(self, t: Target, command_id: str, instance_id: str) -> Invocation | None: ...
    def presign_put(self, bucket: str, key: str, ttl_s: int) -> str: ...
    def presign_get(self, bucket: str, key: str, ttl_s: int) -> str: ...
    def list_objects(self, bucket: str, prefix: str, limit: int) -> list[dict]: ...


class BotoBroker:
    """The real broker: boto3 clients for one region, shared across calls."""

    def __init__(self, region: str):
        import boto3
        from botocore.config import Config

        # Short timeouts and few retries: these calls run inside web requests and
        # the claim tick, and a hung AWS call must cost seconds, not a worker.
        cfg = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2, "mode": "standard"})
        self.region = region
        self._ec2 = boto3.client("ec2", region_name=region, config=cfg)
        self._ssm = boto3.client("ssm", region_name=region, config=cfg)
        self._s3 = boto3.client("s3", region_name=region, config=cfg)
        self._cfn = boto3.client("cloudformation", region_name=region, config=cfg)

    def _inst(self, t: Target) -> EphemeralInstance:
        return EphemeralInstance(region=self.region, launch_template_id=t.launch_template_id,
                                 capability=t.capability, owner=t.owner, ec2=self._ec2, ssm=self._ssm)

    def stack_outputs(self, stack: str) -> dict[str, str]:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            stacks = self._cfn.describe_stacks(StackName=stack).get("Stacks") or []
        except (ClientError, BotoCoreError) as e:
            raise AwsError(f"cannot read stack {stack!r} in {self.region}: {e}") from e
        if not stacks:
            raise AwsError(f"stack {stack!r} does not exist in {self.region}")
        return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs") or []}

    def launch(self, t: Target, *, lease_id: str, user_data: dict) -> str:
        return self._inst(t).launch(lease_id=lease_id, user_data=user_data)

    def describe(self, t: Target, instance_id: str) -> Described | None:
        from canopy_sdk.ondemand import InstanceGone

        try:
            return self._inst(t).describe(instance_id)
        except InstanceGone:
            return None

    def terminate(self, t: Target, instance_ids: list[str]) -> None:
        self._inst(t).terminate(*instance_ids)

    def list_tagged(self, t: Target) -> list[Described]:
        return self._inst(t).list_tagged()

    def read_json(self, bucket: str, key: str) -> dict | None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            body = self._s3.get_object(Bucket=bucket, Key=key)["Body"].read(64 * 1024)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return None
            raise AwsError(f"cannot read s3://{bucket}/{key}: {e}") from e
        except BotoCoreError as e:
            raise AwsError(f"cannot read s3://{bucket}/{key}: {e}") from e
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return {"ok": False, "reason": "ready.json is not JSON"}
        return data if isinstance(data, dict) else {"ok": False, "reason": "ready.json is not an object"}

    def run_async(self, t: Target, instance_id: str, commands: list[str], *, timeout_s: int,
                  output_prefix: str) -> str:
        return self._inst(t).run_async(instance_id, commands, timeout_s=timeout_s,
                                       output_bucket=t.bucket, output_prefix=output_prefix)

    def invocation(self, t: Target, command_id: str, instance_id: str) -> Invocation | None:
        return self._inst(t).invocation(command_id, instance_id)

    def presign_put(self, bucket: str, key: str, ttl_s: int) -> str:
        return self._s3.generate_presigned_url("put_object", Params={"Bucket": bucket, "Key": key},
                                               ExpiresIn=ttl_s)

    def presign_get(self, bucket: str, key: str, ttl_s: int) -> str:
        return self._s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key},
                                               ExpiresIn=ttl_s)

    def list_objects(self, bucket: str, prefix: str, limit: int) -> list[dict]:
        from botocore.exceptions import BotoCoreError, ClientError

        out: list[dict] = []
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        try:
            while len(out) < limit:
                r = self._s3.list_objects_v2(**kwargs, MaxKeys=min(1000, limit - len(out)))
                out.extend({"key": o["Key"], "size": o.get("Size", 0)} for o in r.get("Contents") or [])
                if not r.get("IsTruncated"):
                    break
                kwargs["ContinuationToken"] = r["NextContinuationToken"]
        except (ClientError, BotoCoreError) as e:
            raise AwsError(f"cannot list s3://{bucket}/{prefix}: {e}") from e
        return out


@functools.lru_cache(maxsize=8)
def _boto(region: str) -> BotoBroker:
    return BotoBroker(region)


def get(region: str) -> Broker:
    """The broker for `region`. THE seam tests replace."""
    return _boto(region)


#: What a broker call may raise. Callers that must never fail (the sweep, the
#: turn-terminal receiver) catch these; request handlers turn them into 502s.
ERRORS: tuple[type[BaseException], ...] = (AwsError, OnDemandError)
try:  # botocore is always present with boto3; kept optional for clarity
    from botocore.exceptions import BotoCoreError, ClientError

    ERRORS = ERRORS + (BotoCoreError, ClientError)
except ImportError:  # pragma: no cover
    pass
