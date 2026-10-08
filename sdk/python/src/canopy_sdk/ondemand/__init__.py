"""On-demand dedicated-capability runner (optional: needs the ``ondemand`` extra).

Not imported by ``canopy_sdk`` itself, so the core stays importable without boto3.
"""
from .errors import InstanceGone, OnDemandError, SSMFailure, SSMTimeout
from .instance import OnDemandInstance, Running, Status
from .ssm import CommandResult, run_command

__all__ = [
    "CommandResult", "InstanceGone", "OnDemandError", "OnDemandInstance",
    "Running", "SSMFailure", "SSMTimeout", "Status", "run_command",
]
