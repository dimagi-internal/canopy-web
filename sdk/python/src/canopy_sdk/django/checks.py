"""Django system checks for a canopy host: a setting that would break ``site.json``
is caught when the host starts, not when canopy next fetches the document."""
from __future__ import annotations

from django.core import checks


@checks.register(checks.Tags.compatibility)
def check_resource_schemes(app_configs=None, **kwargs):
    """Every ``CANOPY_HOST["RESOURCE_SCHEMES"]`` entry is a scheme a host may keep
    results under (``canopy_sdk.resources.check_scheme``). One bad entry makes the
    whole signed ``site.json`` malformed, and canopy then reads none of the site's
    facts — not its schemes, origins or keys."""
    from ..resources import ResourceError, check_scheme
    from . import conf

    try:
        schemes = conf.resource_schemes()
    except Exception as exc:  # noqa: BLE001 — an unreadable CANOPY_HOST is its own error
        return [checks.Error(f"CANOPY_HOST could not be read: {exc}", id="canopy_host.E010")]
    errors = []
    for scheme in schemes:
        try:
            check_scheme(scheme)
        except ResourceError as exc:
            errors.append(checks.Error(
                f"CANOPY_HOST['RESOURCE_SCHEMES'] has {scheme!r}: {exc.message}",
                hint="Give your results a scheme of your own, e.g. 'myapp-result'. Until this is "
                     "fixed site.json is malformed and canopy reads none of it.",
                id="canopy_host.E011"))
    return errors
