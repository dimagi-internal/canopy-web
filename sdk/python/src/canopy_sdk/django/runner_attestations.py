"""Runner attestations for a Django host, configured from ``CANOPY_HOST``
(``canopy_sdk.runner_attestations``).

::

    from canopy_sdk.django import runner_attestations

    kind, proof = runner_attestations.parse_header(request.headers["X-Canopy-Runner-Proof"])
    got = runner_attestations.attest(kind=kind, proof=proof, speaks_for="ace")
    # got.runner, got.serves_agent ...

Signs as ``APP_NAME`` with ``SIGNING_KEY`` at ``CANOPY_BASE_URL``, naming the tenant like
``canopy_sdk.django.sign_offs``. Every failure is an ``AttestationError``; ``not_configured``
when a setting is missing.
"""
from __future__ import annotations

from ..host.config import HostNotConfigured
from ..runner_attestations import (  # noqa: F401 - the host's whole surface
    HEADER,
    Attestation,
    AttestationError,
    bound_url,
    parse_header,
    request_attestation,
)
from ..sign_offs import NOT_CONFIGURED
from . import conf
from .site_agents import _selector


def attest(*, kind: str, proof: str, speaks_for: str = "", post=None) -> Attestation:
    """Ask canopy who made ``proof`` (a runner's, for a request to this host)."""
    try:
        config = conf.get_host_config()
    except HostNotConfigured as exc:
        raise AttestationError(NOT_CONFIGURED, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a malformed key or setting
        raise AttestationError(NOT_CONFIGURED, f"CANOPY_HOST is not usable: {type(exc).__name__}") from exc
    if not config.assertions_enabled:
        raise AttestationError(NOT_CONFIGURED, "CANOPY_HOST needs CANOPY_BASE_URL and APP_NAME to ask canopy")
    workspace, agent = _selector(config)
    return request_attestation(config.canopy_base_url, config.app_name, config.signing_key, kind=kind, proof=proof,
                               speaks_for=speaks_for, workspace=workspace, agent=agent, post=post)
