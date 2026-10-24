"""Django system checks for a canopy host: a setting that would break ``site.json``
is caught when the host starts, not when canopy next fetches the document."""
from __future__ import annotations

from django.core import checks


@checks.register(checks.Tags.compatibility)
def check_resource_schemes(app_configs=None, **kwargs):
    """Every ``CANOPY_HOST["RESOURCE_SCHEMES"]`` entry is a scheme a host may keep
    results under (``canopy_sdk.resources.check_scheme``), and every
    ``RESOURCE_ID_LENGTHS`` entry names one of them with a length
    ``check_id_length`` takes. One bad entry makes the whole signed ``site.json``
    malformed, and canopy then reads none of the site's facts — not its schemes,
    origins or keys."""
    from collections.abc import Mapping

    from ..resources import ResourceError, check_id_length, check_scheme
    from . import conf

    try:
        schemes = conf.resource_schemes()
        lengths = conf.resource_id_lengths()
    except Exception as exc:  # noqa: BLE001 — an unreadable CANOPY_HOST is its own error
        return [checks.Error(f"CANOPY_HOST could not be read: {exc}", id="canopy_host.E010")]
    hint = "Until this is fixed site.json is malformed and canopy reads none of it."
    errors = []
    good = set()
    for scheme in schemes:
        try:
            good.add(check_scheme(scheme))
        except ResourceError as exc:
            errors.append(checks.Error(
                f"CANOPY_HOST['RESOURCE_SCHEMES'] has {scheme!r}: {exc.message}",
                hint="Give your results a scheme of your own, e.g. 'myapp-result'. " + hint,
                id="canopy_host.E011"))
    if not isinstance(lengths, Mapping):
        return errors + [checks.Error(
            "CANOPY_HOST['RESOURCE_ID_LENGTHS'] must map a scheme to the length of its ids",
            hint="e.g. {'myapp-result': 24} for secrets.token_urlsafe(18). " + hint, id="canopy_host.E011")]
    for scheme, length in lengths.items():
        key = str(scheme or "").strip().lower()
        if key not in good:
            errors.append(checks.Error(
                f"CANOPY_HOST['RESOURCE_ID_LENGTHS'] names {scheme!r}, which is not in RESOURCE_SCHEMES",
                hint=hint, id="canopy_host.E011"))
            continue
        try:
            check_id_length(length)
        except ResourceError as exc:
            errors.append(checks.Error(
                f"CANOPY_HOST['RESOURCE_ID_LENGTHS'][{scheme!r}]: {exc.message}",
                hint="The length of the url-safe id after '<scheme>://', e.g. 24 for "
                     "secrets.token_urlsafe(18). " + hint, id="canopy_host.E011"))
    return errors
