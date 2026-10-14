"""A stand-in for AWS, installed through `aws.get` — the one seam capabilities uses.

It models what the broker relies on AWS for: RunInstances' ClientToken
idempotency, the instances' tags, S3 objects (ready.json, artifacts) and SSM
invocations. State lives in `FakeAws`, never in the broker object, so a test
can swap in a FRESH broker (a new canopy-web process) and read the same world.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from canopy_sdk.ondemand import LIVE_STATES, Described, Invocation, LaunchFailed

from apps.capabilities import aws

OUTPUTS = {
    "LaunchTemplateId": "lt-0abc",
    "ArtifactsBucketName": "cap-bucket",
    "InstanceRoleArn": "arn:aws:iam::1:role/cap-instance",
    "InstanceRoleId": "AROAEXAMPLE",
    "Capability": "android-emulator",
    "Owner": "ace",
}


@dataclass
class FakeAws:
    stacks: dict[str, dict] = field(default_factory=lambda: {"canopy-cap-ace-android-emulator": dict(OUTPUTS)})
    instances: dict[str, Described] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)  # ClientToken -> instance id
    run_calls: list[dict] = field(default_factory=list)
    objects: dict[tuple[str, str], object] = field(default_factory=dict)  # (bucket, key) -> json or size
    invocations: dict[tuple[str, str], Invocation] = field(default_factory=dict)
    sent: list[dict] = field(default_factory=list)
    terminated: list[str] = field(default_factory=list)
    launch_error: Exception | None = None
    list_error: Exception | None = None
    stack_error: Exception | None = None
    _seq: int = 0

    def broker(self) -> "FakeBroker":
        return FakeBroker(self)

    def add_instance(self, *, lease_id: str = "", capability: str = "android-emulator", owner: str = "ace",
                     state: str = "running") -> str:
        self._seq += 1
        iid = f"i-{self._seq:04d}"
        tags = {"canopy:capability": capability, "canopy:owner": owner}
        if lease_id:
            tags["canopy:lease"] = lease_id
        self.instances[iid] = Described(iid, state, dt.datetime.now(dt.timezone.utc), tags)
        return iid

    def ready(self, iid: str, **body) -> None:
        self.objects[("cap-bucket", f"vm/AROAEXAMPLE:{iid}/ready.json")] = {"ok": True, **body}

    def live(self) -> list[str]:
        return [i for i, d in self.instances.items() if d.state in LIVE_STATES]


class FakeBroker:
    def __init__(self, world: FakeAws):
        self.w = world

    def stack_outputs(self, stack):
        if self.w.stack_error:
            raise self.w.stack_error
        if stack not in self.w.stacks:
            raise aws.AwsError(f"stack {stack!r} does not exist in us-east-1")
        return dict(self.w.stacks[stack])

    def launch(self, t, *, lease_id, user_data):
        self.w.run_calls.append({"lease_id": lease_id, "user_data": user_data, "target": t})
        if self.w.launch_error:
            raise self.w.launch_error
        if lease_id in self.w.tokens:  # RunInstances' ClientToken idempotency
            return self.w.tokens[lease_id]
        iid = self.w.add_instance(lease_id=lease_id, capability=t.capability, owner=t.owner, state="pending")
        self.w.tokens[lease_id] = iid
        return iid

    def describe(self, t, instance_id):
        return self.w.instances.get(instance_id)

    def terminate(self, t, instance_ids):
        for iid in instance_ids:
            self.w.terminated.append(iid)
            if iid in self.w.instances:
                self.w.instances[iid].state = "shutting-down"

    def list_tagged(self, t):
        if self.w.list_error:
            raise self.w.list_error
        return [d for d in self.w.instances.values() if d.state in LIVE_STATES
                and d.tags.get("canopy:capability") == t.capability and d.tags.get("canopy:owner") == t.owner]

    def read_json(self, bucket, key):
        v = self.w.objects.get((bucket, key))
        return v if isinstance(v, dict) else None

    def run_async(self, t, instance_id, commands, *, timeout_s, output_prefix):
        cid = f"00000000-0000-4000-8000-{len(self.w.sent) + 1:012d}"
        self.w.sent.append({"instance_id": instance_id, "commands": commands, "timeout_s": timeout_s,
                            "output_prefix": output_prefix, "command_id": cid})
        self.w.invocations[(cid, instance_id)] = Invocation(cid, instance_id, "InProgress", None, "", "")
        return cid

    def invocation(self, t, command_id, instance_id):
        return self.w.invocations.get((command_id, instance_id))

    def presign_put(self, bucket, key, ttl_s):
        return f"https://signed.example/{bucket}/{key}?put&ttl={ttl_s}"

    def presign_get(self, bucket, key, ttl_s):
        return f"https://signed.example/{bucket}/{key}?get&ttl={ttl_s}"

    def list_objects(self, bucket, prefix, limit):
        return [{"key": k, "size": v} for (b, k), v in sorted(self.w.objects.items())
                if b == bucket and k.startswith(prefix) and isinstance(v, int)][:limit]


def transient(message="capacity") -> LaunchFailed:
    return LaunchFailed(message, code="InsufficientInstanceCapacity", transient=True)
