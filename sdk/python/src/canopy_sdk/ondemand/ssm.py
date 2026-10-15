"""Thin boto3 wrapper around AWS SSM ``send_command`` + poll.

Ported from ace-web's ``apps/mobile/ssm.py``. SSM rather than SSH so a
caller can drive the EC2 instance over the AWS control plane with no
inbound SG rules or key management; the instance role needs the SSM agent's
grants (``AmazonSSMManagedInstanceCore`` in persistent mode; an ephemeral
capability stack carries the agent's needs without its ``ssm:GetParameter*``).

Module is sync. Polling is a 1 s loop by default: commands return in
hundreds of ms typically but may run for tens of seconds.

One retry on ``ThrottlingException`` because SSM's TPS can dip; otherwise
errors surface to the caller as ``SSMFailure``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from .errors import SSMFailure, SSMTimeout

# SSM command status set per
# https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_GetCommandInvocation.html
_TERMINAL_STATUSES = {"Success", "Cancelled", "TimedOut", "Failed"}
_FAILURE_STATUSES = {"Cancelled", "TimedOut", "Failed"}


@dataclass
class CommandResult:
    """Result of a single SSM ``AWS-RunShellScript`` invocation."""

    stdout: str
    stderr: str
    status: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.status == "Success" and self.exit_code == 0


def run_command(
    ssm_client: Any,
    instance_id: str,
    *,
    commands: list[str],
    timeout_seconds: int = 600,
    poll_interval: float = 1.0,
    return_on_script_failure: bool = False,
) -> CommandResult:
    """Send a shell command to ``instance_id`` and wait for completion.

    Args:
        ssm_client: a boto3 SSM client (``boto3.client("ssm")``). Passed
            in so callers can inject a stubbed client in tests.
        instance_id: target EC2 instance.
        commands: list of shell command strings; concatenated into a
            single ``AWS-RunShellScript`` invocation.
        timeout_seconds: outer wall-clock timeout. Includes both the SSM
            send-command + the in-VM execution. Beyond this we raise
            ``SSMTimeout`` even if SSM is still polling.
        poll_interval: seconds between ``get_command_invocation`` polls.

    Raises:
        SSMTimeout: if ``timeout_seconds`` elapses before terminal status.
        SSMFailure: on non-throttling boto errors, or on terminal
            failure statuses (``Cancelled``, ``TimedOut``, ``Failed``).
    """
    command_id = _send_with_retry(
        ssm_client, instance_id, commands, timeout_seconds
    )

    deadline = time.monotonic() + timeout_seconds
    last_response: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            resp = ssm_client.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("InvocationDoesNotExist", "InvalidCommandId"):
                # SSM hasn't materialized the invocation yet; keep polling.
                time.sleep(poll_interval)
                continue
            raise SSMFailure(f"SSM get_command_invocation failed: {e}") from e

        last_response = resp
        status = resp.get("Status", "Pending")
        if status in _TERMINAL_STATUSES:
            return _build_result(resp, return_on_script_failure=return_on_script_failure)
        time.sleep(poll_interval)

    raise SSMTimeout(
        f"SSM command {command_id} on {instance_id} did not finish in "
        f"{timeout_seconds}s; last status: "
        f"{(last_response or {}).get('Status', 'unknown')}"
    )


def _send_with_retry(
    ssm_client: Any,
    instance_id: str,
    commands: list[str],
    timeout_seconds: int,
) -> str:
    """Call ``send_command`` with one retry on throttling."""
    return send_command(ssm_client, instance_id, commands=commands, timeout_seconds=timeout_seconds)


def send_command(
    ssm_client: Any,
    instance_id: str,
    *,
    commands: list[str],
    timeout_seconds: int = 600,
    execution_timeout_s: int | None = None,
    output_bucket: str | None = None,
    output_prefix: str | None = None,
    comment: str | None = None,
    document_name: str = "AWS-RunShellScript",
    parameters: dict[str, list[str]] | None = None,
    document_version: str | None = None,
) -> str:
    """Send a command document to ``instance_id`` and return the CommandId at once.

    The fire-and-forget half of ``run_command``: nothing is polled, so the
    command's state lives in SSM and a caller that restarts can read it back
    with ``get_command_invocation``. One retry on ``ThrottlingException``.

    By default the document is ``AWS-RunShellScript`` with ``commands`` as its
    script, and ``execution_timeout_s`` bounds the script on the instance (the
    document's ``executionTimeout``). With ``parameters`` the call sends
    ``document_name`` with exactly those parameters instead — ``commands`` and
    ``execution_timeout_s`` are then ignored, since another document names its
    own (e.g. a capability stack's exec document, which takes an argv and runs
    it only if argv[0] is on its allow list). ``timeout_seconds`` is SSM's
    delivery timeout either way. With ``output_bucket``, SSM's agent also writes
    the full stdout and stderr to
    ``s3://<output_bucket>/<output_prefix>/<CommandId>/<instance-id>/...``
    (the instance role needs ``s3:PutObject`` there).
    """
    if parameters is not None:
        params: dict[str, list[str]] = {k: [str(x) for x in v] for k, v in parameters.items()}
    else:
        params = {"commands": commands}
        if execution_timeout_s is not None:
            params["executionTimeout"] = [str(max(1, int(execution_timeout_s)))]
    kwargs: dict[str, Any] = {
        "InstanceIds": [instance_id],
        "DocumentName": document_name,
        "Parameters": params,
        "TimeoutSeconds": max(30, min(timeout_seconds, 2592000)),
    }
    if output_bucket:
        kwargs["OutputS3BucketName"] = output_bucket
        if output_prefix:
            kwargs["OutputS3KeyPrefix"] = output_prefix.strip("/")
    if document_version:
        kwargs["DocumentVersion"] = document_version
    if comment:
        kwargs["Comment"] = comment[:100]
    attempts = 0
    while True:
        attempts += 1
        try:
            resp = ssm_client.send_command(**kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ThrottlingException" and attempts == 1:
                time.sleep(2.0)
                continue
            raise SSMFailure(f"SSM send_command failed: {e}") from e

        command = resp.get("Command") or {}
        command_id = command.get("CommandId")
        if not command_id:
            raise SSMFailure(
                f"SSM send_command returned no CommandId: {resp!r}"
            )
        return command_id


def _build_result(
    resp: dict[str, Any],
    *,
    return_on_script_failure: bool = False,
) -> CommandResult:
    status = resp.get("Status", "Failed")
    raw_code = resp.get("ResponseCode")
    exit_code = int(raw_code if raw_code is not None else 1)
    stdout = resp.get("StandardOutputContent", "") or ""
    stderr = resp.get("StandardErrorContent", "") or ""

    if status in _FAILURE_STATUSES:
        # ``return_on_script_failure`` only forgives ``Failed`` with a
        # non-negative exit_code — that's a script that ran to completion
        # and exited non-zero (a parse error, assertion failure, ...). ``Cancelled``, ``TimedOut``, and ``Failed``
        # with exit_code < 0 (signal-killed: instance terminated mid-run,
        # SSM agent crash) are infrastructure failures and still raise.
        # The caller can't produce a meaningful result envelope from
        # those, but it CAN from a clean recipe failure.
        script_failed_cleanly = (
            return_on_script_failure
            and status == "Failed"
            and exit_code >= 0
        )
        if not script_failed_cleanly:
            # Terminal failure — surface as SSMFailure so the view returns 502.
            # We still package the streams in the exception message so callers
            # can see what the in-VM command spat out before bailing.
            raise SSMFailure(
                f"SSM command terminated with status={status} exit_code={exit_code}; "
                f"stderr={stderr[:500]!r}"
            )

    return CommandResult(
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    )
