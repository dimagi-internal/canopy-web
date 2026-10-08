"""Errors raised by the on-demand runner."""
from __future__ import annotations


class OnDemandError(RuntimeError):
    """An on-demand capability runner could not do what was asked."""


class InstanceGone(OnDemandError):
    """The configured instance no longer exists; only a redeploy recovers it."""


class SSMFailure(OnDemandError):
    """An SSM call failed, or the command reached a terminal failure status."""


class SSMTimeout(OnDemandError):
    """An SSM command did not reach a terminal status in time."""
