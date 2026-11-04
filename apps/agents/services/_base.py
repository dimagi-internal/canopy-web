from __future__ import annotations

import datetime as dt

from django.utils import timezone


def _aware(value):
    if isinstance(value, dt.datetime) and timezone.is_naive(value):
        return value.replace(tzinfo=dt.UTC)
    return value
