"""Site sign-offs for a Django host, configured from ``CANOPY_HOST`` (``canopy_sdk.sign_offs``).

::

    from canopy_sdk.django import sign_offs

    asked = sign_offs.request(subject=user.email, action_key="labs.mcp_token.full",
                              title="Create a full-access labs MCP token", site_ref=pending.ref,
                              return_url=request.build_absolute_uri(reverse("...")))
    return redirect(asked.approve_url)

    # back at return_url?sign_off=<jws>&id=<id>
    approval = sign_offs.verify(request.GET["sign_off"], action_key="labs.mcp_token.full",
                                site_ref=pending.ref, subject=user.email)

    state = sign_offs.status(asked.id)      # when someone else approves

It signs with ``CANOPY_HOST``'s ``SIGNING_KEY`` as ``APP_NAME``, at ``CANOPY_BASE_URL``,
naming the tenant by ``WORKSPACE`` (or ``AGENT_SLUG``), exactly as the site agents
request does, and checks canopy's key through the shared ``ClientKeyResolver``. Every
failure is a ``SignOffError``; ``not_configured`` when a setting is missing.

``verify`` does not spend the approval's ``jti``: the host does, in the same
transaction that acts on it, so a crash between the two cannot leave an approval spent
and nothing done (or the reverse).
"""
from __future__ import annotations

from .. import contract
from ..host.client_keys import ClientKeyResolver
from ..host.config import HostNotConfigured
from ..sign_offs import (
    NOT_CONFIGURED,
    Approval,
    SignOffError,
    SignOffRequest,
    SignOffStatus,
    fetch_status,
    request_sign_off,
    verify_approval,
)
from . import conf
from .site_agents import _selector


def _config():
    try:
        config = conf.get_host_config()
    except HostNotConfigured as exc:
        raise SignOffError(NOT_CONFIGURED, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a malformed key or setting
        raise SignOffError(NOT_CONFIGURED, f"CANOPY_HOST is not usable: {type(exc).__name__}") from exc
    if not config.assertions_enabled:
        raise SignOffError(NOT_CONFIGURED, "CANOPY_HOST needs CANOPY_BASE_URL and APP_NAME to ask for a sign-off")
    return config


def request(*, subject: str, action_key: str, title: str, site_ref: str, return_url: str,
            detail: str = "", post=None) -> SignOffRequest:
    """Ask canopy for ``subject``'s passkey approval of one action."""
    config = _config()
    workspace, agent = _selector(config)
    return request_sign_off(config.canopy_base_url, config.app_name, config.signing_key,
                            subject=subject, action_key=action_key, title=title, site_ref=site_ref,
                            return_url=return_url, detail=detail, workspace=workspace, agent=agent,
                            post=post)


def status(sign_off_id: str, *, get=None) -> SignOffStatus:
    """The state of a request this host made, with the approval once there is one."""
    config = _config()
    workspace, agent = _selector(config)
    return fetch_status(config.canopy_base_url, config.app_name, config.signing_key, sign_off_id,
                        workspace=workspace, agent=agent, get=get)


def verify(token: str, *, action_key: str, site_ref: str, subject: str | None = None,
           client_keys=None) -> Approval:
    """Check an approval canopy signed for this host. The caller spends its ``jti``."""
    from django.core.cache import cache

    config = _config()
    return verify_approval(token, client_id=config.canopy_client_id or contract.client_id_for(config.canopy_base_url),
                           site_name=config.app_name, client_keys=client_keys or ClientKeyResolver(cache=cache),
                           expect_action=action_key, expect_site_ref=site_ref, expect_subject=subject)
