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
#: What a 0.9.0 stack adds: per-instance object prefixes, the exec document, the backstop.
OUTPUTS_V09 = {
    **OUTPUTS,
    "LeaseObjectScope": "instance",
    "ExecDocumentName": "canopy-cap-ace-android-emulator-exec",
    "ExecAllow": "ace-mobile",
    "MaxInstanceLifetimeSeconds": "16200",
    "BackstopFunctionName": "canopy-cap-ace-android-emu-BackstopFunction-x",
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
    terminate_error: Exception | None = None
    #: Called inside launch (before it answers): a release racing the launch.
    launch_hook: object = None
    #: Called once, inside list_tagged after the snapshot: a racing process.
    list_hook: object = None
    read_hook: object = None
    invocation_hook: object = None
    credential_ttl_s: int = 0
    list_calls: int = 0
    describe_calls: int = 0
    json_reads: int = 0
    invocation_reads: int = 0
    _seq: int = 0

    def broker(self) -> FakeBroker:
        return FakeBroker(self)

    def add_instance(self, *, lease_id: str = "", capability: str = "android-emulator", owner: str = "ace",
                     state: str = "running", broker: str | None = None) -> str:
        self._seq += 1
        iid = f"i-{self._seq:04d}"
        tags = {"canopy:capability": capability, "canopy:owner": owner}
        if lease_id:
            tags["canopy:lease"] = lease_id
        if broker:
            tags["canopy:broker"] = broker
        self.instances[iid] = Described(iid, state, dt.datetime.now(dt.UTC), tags)
        return iid

    def v09(self) -> None:
        """Upgrade the stack to 0.9.0's outputs (the broker sees it on its next read)."""
        self.stacks["canopy-cap-ace-android-emulator"] = dict(OUTPUTS_V09)

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

    def launch(self, t, *, lease_id, user_data, tags):
        self.w.run_calls.append({"lease_id": lease_id, "user_data": user_data, "target": t, "tags": tags})
        if self.w.launch_hook:
            self.w.launch_hook(lease_id)
        if self.w.launch_error:
            raise self.w.launch_error
        if lease_id in self.w.tokens:  # RunInstances' ClientToken idempotency
            return self.w.tokens[lease_id]
        iid = self.w.add_instance(lease_id=lease_id, capability=t.capability, owner=t.owner, state="pending",
                                  broker=tags.get("canopy:broker"))
        self.w.tokens[lease_id] = iid
        return iid

    def describe(self, t, instance_id):
        self.w.describe_calls += 1
        return self.w.instances.get(instance_id)

    def terminate(self, t, instance_ids):
        if self.w.terminate_error:
            raise self.w.terminate_error
        for iid in instance_ids:
            self.w.terminated.append(iid)
            if iid in self.w.instances:
                self.w.instances[iid].state = "shutting-down"

    def list_tagged(self, t):
        self.w.list_calls += 1
        if self.w.list_hook:
            hook, self.w.list_hook = self.w.list_hook, None
            snapshot = [d for d in self.w.instances.values() if d.state in LIVE_STATES
                        and d.tags.get("canopy:capability") == t.capability and d.tags.get("canopy:owner") == t.owner]
            hook()  # another process runs here, after our read
            return snapshot
        if self.w.list_error:
            raise self.w.list_error
        return [d for d in self.w.instances.values() if d.state in LIVE_STATES
                and d.tags.get("canopy:capability") == t.capability and d.tags.get("canopy:owner") == t.owner]

    def read_json(self, bucket, key):
        self.w.json_reads += 1
        if self.w.read_hook:
            self.w.read_hook(self.w.json_reads)
        v = self.w.objects.get((bucket, key))
        return v if isinstance(v, dict) else None

    def run_async(self, t, instance_id, commands, *, timeout_s, output_prefix):
        cid = f"00000000-0000-4000-8000-{len(self.w.sent) + 1:012d}"
        self.w.sent.append({"instance_id": instance_id, "commands": commands, "timeout_s": timeout_s,
                            "output_prefix": output_prefix, "command_id": cid})
        self.w.invocations[(cid, instance_id)] = Invocation(cid, instance_id, "InProgress", None, "", "")
        return cid

    def run_document(self, t, instance_id, parameters, *, timeout_s, output_prefix):
        cid = f"00000000-0000-4000-8000-{len(self.w.sent) + 1:012d}"
        self.w.sent.append({"instance_id": instance_id, "document": t.exec_document, "parameters": parameters,
                            "timeout_s": timeout_s, "output_prefix": output_prefix, "command_id": cid})
        self.w.invocations[(cid, instance_id)] = Invocation(cid, instance_id, "InProgress", None, "", "")
        return cid

    def presign_ttl(self, ttl_s):
        return min(ttl_s, self.w.credential_ttl_s) if self.w.credential_ttl_s else ttl_s

    def invocation(self, t, command_id, instance_id):
        self.w.invocation_reads += 1
        if self.w.invocation_hook:
            self.w.invocation_hook(self.w.invocation_reads)
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
