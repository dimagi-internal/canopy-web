"""On-demand dedicated-capability runner (optional: needs the ``ondemand`` extra).

Not imported by ``canopy_sdk`` itself, so the core stays importable without boto3.
"""
from .ephemeral import (
    LIVE_STATES,
    TAG_BROKER,
    TAG_CAPABILITY,
    TAG_LEASE,
    TAG_OWNER,
    Described,
    EphemeralInstance,
    Invocation,
    LaunchFailed,
)
from .errors import InstanceGone, OnDemandError, SSMFailure, SSMTimeout
from .instance import OnDemandInstance, Running, Status
from .jobs import Job, JobStore
from .lease import Lease
from .ssm import CommandResult, run_command, send_command

__all__ = [
    "CommandResult", "Described", "EphemeralInstance", "InstanceGone", "Invocation", "Job",
    "JobStore", "LIVE_STATES", "LaunchFailed", "Lease", "OnDemandError", "OnDemandInstance",
    "Running", "SSMFailure", "SSMTimeout", "Status", "TAG_BROKER", "TAG_CAPABILITY", "TAG_LEASE", "TAG_OWNER",
    "run_command", "send_command",
]
