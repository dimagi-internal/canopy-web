"""The host keeps canopy's copy of its people current, and makes canopy forget them.

A host site is the SYSTEM OF RECORD for the people it brings to canopy. canopy keeps an
anchor per person (keyed by the host's own id, the visitor assertion's ``sub``), a
pointer to the host's record (``record_uri``, under ``CANOPY_HOST["PERSON_SCHEME"]`` —
declared in ``site.json`` as ``site.person_scheme``) and a small profile the host owns.

    from canopy_sdk.host.contacts import erase_contacts, update_contacts

    update_contacts(config, [{"sub": "personalid:ana", "display_name": "Ana",
                              "record_uri": "labs-worker://ana",
                              "profile": {"stage": "deliver", "language": "fr"},
                              "version": 17}])
    # {"results": [{"sub": "personalid:ana", "contact_id": 812, "status": "updated"}]}

    erase_contacts(config, ["personalid:ana"])
    # {"results": [{"sub": "personalid:ana", "status": "erased"}]}

``version`` is yours: any whole number you increase with each change (a row version, a
timestamp in ms). canopy overwrites the three fields only when it is newer than the one
it holds; an older or equal one is reported ``stale`` and changes nothing, so retries
and out-of-order deliveries are safe. A missing person is created (``created``), so you
can register people before they first write in.

``config`` is the host's ``HostConfig``: its ``signing_key``, ``app_name``,
``canopy_base_url`` and ``workspace``. Each request is a compact JWS signed by that key
(the one canopy verifies your visitor assertions with), sent as the body. Raises
``canopy_sdk.contacts.ContactRecordError`` (``unreachable``, ``not_configured``, or
canopy's refusal code).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence

from .. import contacts as wire
from .config import HostConfig

__all__ = ["erase_contacts", "update_contacts"]


def _ready(config: HostConfig) -> None:
    if not (config.canopy_base_url and config.app_name):
        raise wire.ContactRecordError("not_configured", "the host config needs canopy_base_url and app_name")


def update_contacts(config: HostConfig, contacts: Sequence[Mapping], *,
                    post: Callable[..., tuple] | None = None) -> dict:
    """Send up to 100 ``{sub, display_name?, record_uri?, profile?, version}`` items.
    Returns canopy's ``{"results": [{sub, contact_id, status, reason?}]}`` — ``status``
    one of ``created``, ``updated``, ``stale``, ``refused`` (then ``reason`` says why)."""
    _ready(config)
    url = wire.update_url(config.canopy_base_url)
    token = wire.update_token(config.app_name, config.signing_key, url, list(contacts),
                              workspace=config.workspace)
    return wire.post(url, token, sender=post)


def erase_contacts(config: HostConfig, subs: Iterable[str], *,
                   post: Callable[..., tuple] | None = None) -> dict:
    """Make canopy forget up to 100 people. Returns ``{"results": [{sub, status}]}``,
    ``status`` ``erased`` or ``not_found``. Idempotent: a second erase is ``not_found``."""
    _ready(config)
    url = wire.erase_url(config.canopy_base_url)
    token = wire.erase_token(config.app_name, config.signing_key, url, list(subs),
                             workspace=config.workspace)
    return wire.post(url, token, sender=post)
