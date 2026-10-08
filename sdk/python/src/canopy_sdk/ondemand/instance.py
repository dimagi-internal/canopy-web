"""One stopped-when-idle EC2 instance that hosts a dedicated capability.

Extracted from ace-web's cloud mobile runner (apps/mobile/controller.py):
start on demand, wait for EC2 status checks, wait for the capability's own
ready file over SSM, and never terminate -- idle machinery only stops it.
"""
from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field

import boto3
from botocore.exceptions import ClientError, WaiterError

from .errors import InstanceGone, OnDemandError, SSMFailure, SSMTimeout
from .ssm import CommandResult, run_command


@dataclass
class Running:
    instance_id: str
    cold: bool
    timings: dict[str, float] = field(default_factory=dict)


@dataclass
class Status:
    instance_id: str
    state: str | None
    idle_for_s: int | None


class OnDemandInstance:
    def __init__(self, instance_id, region, capability, *, ready_file=None, ec2=None, ssm=None):
        self.instance_id = instance_id
        self.capability = capability
        self.ready_file = ready_file or f"/run/{capability}/ready"
        self.activity_file = f"/var/run/{capability}/last-activity"
        self.ec2 = ec2 or boto3.client("ec2", region_name=region)
        self.ssm = ssm or boto3.client("ssm", region_name=region)

    def _state(self) -> str:
        try:
            r = self.ec2.describe_instances(InstanceIds=[self.instance_id])
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"):
                raise InstanceGone(self._gone_msg("missing")) from e
            raise OnDemandError(f"describe_instances failed for {self.instance_id}: {e}") from e
        insts = [i for res in r.get("Reservations", []) for i in res.get("Instances", [])]
        if not insts:
            raise InstanceGone(self._gone_msg("missing"))
        return insts[0]["State"]["Name"]

    def _gone_msg(self, state: str) -> str:
        return (f"{self.capability} runner {self.instance_id} is {state!r}. Idle machinery only stops "
                f"it, so something external removed it: redeploy the {self.capability} CloudFormation "
                f"stack and update the consumer's instance-id setting.")

    def ensure_running(self, boot_timeout_s: int = 420, ready_timeout_s: int = 480) -> Running:
        timings: dict[str, float] = {}
        state = self._state()
        cold = state != "running"
        t = time.monotonic()
        if state in ("terminated", "shutting-down"):
            raise InstanceGone(self._gone_msg(state))
        if state not in ("running", "stopped", "pending", "stopping"):
            raise OnDemandError(f"{self.instance_id} in unexpected state {state!r}")
        if state == "stopping":
            self._wait_stopped(boot_timeout_s)
            state = "stopped"
        if state == "stopped":
            try:
                self.ec2.start_instances(InstanceIds=[self.instance_id])
            except ClientError as e:
                raise OnDemandError(f"start_instances failed for {self.instance_id}: {e}") from e
        if cold:
            self._wait_ec2_ok(boot_timeout_s)
            timings["ec2_start_s"] = round(time.monotonic() - t, 1)
        t = time.monotonic()
        self._wait_ready(ready_timeout_s)
        timings["ready_wait_s"] = round(time.monotonic() - t, 1)
        self.touch()
        return Running(self.instance_id, cold, timings)

    def state(self) -> str:
        """The EC2 state name ("running", "stopped", ...): one describe call, no SSM.

        Raises InstanceGone if the instance no longer exists. Use status() when you
        also need the idle age (that costs an SSM round-trip on a running box).
        """
        return self._state()

    def wait_ec2_ok(self, timeout_s: int) -> None:
        """Block until instance AND system status checks pass, within timeout_s."""
        self._wait_ec2_ok(timeout_s)

    def _wait_ec2_ok(self, timeout_s: int) -> None:
        # Both reachability checks, as ace-web's mobile runner always required: the
        # instance_status_ok waiter alone ignores system status. One shared budget.
        # A real m8i.xlarge took ~230 s from start to "ok" (2026-10-08), so the
        # default budget is 420 s, not 180.
        deadline = time.monotonic() + timeout_s
        self._wait("instance_status_ok", timeout_s)
        self._wait("system_status_ok", max(5, int(deadline - time.monotonic())))

    def _wait_stopped(self, timeout_s: int) -> None:
        self._wait("instance_stopped", timeout_s)

    def _wait(self, waiter: str, timeout_s: int) -> None:
        try:
            self.ec2.get_waiter(waiter).wait(
                InstanceIds=[self.instance_id],
                WaiterConfig={"Delay": 5, "MaxAttempts": max(1, timeout_s // 5)},
            )
        except (WaiterError, ClientError) as e:
            raise OnDemandError(f"{waiter} wait failed for {self.instance_id}: {e}") from e

    def _wait_ready(self, timeout_s: int) -> None:
        deadline = time.monotonic() + timeout_s
        probe = f"test -f {shlex.quote(self.ready_file)} && echo READY || echo WAIT"
        last_err = None
        while time.monotonic() < deadline:
            try:
                res = self.run([probe], timeout_s=30)
                if "READY" in (res.stdout or ""):
                    return
            except (SSMFailure, SSMTimeout) as e:
                last_err = e  # SSM agent may not be registered yet right after boot
            time.sleep(5)
        detail = f"; last error: {last_err}" if last_err else ""
        raise OnDemandError(
            f"{self.capability} not ready after {timeout_s}s ({self.ready_file} absent){detail}"
        )

    def touch(self) -> None:
        q = shlex.quote(self.activity_file)
        self.run([f"mkdir -p $(dirname {q}) && touch {q}"], timeout_s=30)

    def run(self, commands: list[str], timeout_s: int = 600) -> CommandResult:
        return run_command(self.ssm, self.instance_id, commands=commands, timeout_seconds=timeout_s)

    def stop(self) -> None:
        try:
            self.ec2.stop_instances(InstanceIds=[self.instance_id])
        except ClientError as e:
            raise OnDemandError(f"stop_instances failed for {self.instance_id}: {e}") from e

    def status(self) -> Status:
        state = self._state()
        idle = None
        if state == "running":
            q = shlex.quote(self.activity_file)
            res = self.run(
                [f"test -f {q} && echo $(( $(date +%s) - $(stat -c %Y {q}) )) || echo NONE"],
                timeout_s=30,
            )
            try:
                idle = int((res.stdout or "").strip())
            except ValueError:
                idle = None
        return Status(self.instance_id, state, idle)
