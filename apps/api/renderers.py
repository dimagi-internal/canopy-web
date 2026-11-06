"""orjson-backed renderer + problem+json content-type override."""
from __future__ import annotations

import json

import orjson
from django.core.serializers.json import DjangoJSONEncoder
from ninja.renderers import BaseRenderer


def orjson_bytes(data) -> bytes:
    """`data` as orjson writes it. Raises `orjson.JSONEncodeError` on what orjson
    cannot write: an integer past 64 bits, or nesting deeper than 254."""
    return orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_UTC_Z)


class OrjsonRenderer(BaseRenderer):
    media_type = "application/json"

    def render(self, request, data, *, response_status):
        try:
            return orjson_bytes(data)
        except orjson.JSONEncodeError:
            # Free-form JSON a caller stored (a jsonb payload) may hold what orjson
            # refuses — JSON allows an integer of any size and PostgreSQL keeps it.
            # The standard encoder writes it; it is slower, so it is only the fallback.
            return json.dumps(data, cls=DjangoJSONEncoder, ensure_ascii=False).encode()


class ProblemJsonRenderer(OrjsonRenderer):
    """Used by the global error handler — sets `application/problem+json`."""

    media_type = "application/problem+json"
