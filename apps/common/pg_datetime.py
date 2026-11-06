"""Write an aware datetime to PostgreSQL whatever its UTC offset.

ISO 8601 and Python allow a UTC offset up to ±23:59; PostgreSQL reads one only
up to ±15:59 and refuses the rest ("time zone displacement out of range"). An
API caller may send `2026-01-01T07:00:00+17:10`, which is a valid instant, and
it must be stored, not refused. psycopg writes an aware datetime with its own
offset, so a dumper that first moves such a value to UTC — the same instant,
and `timestamptz` keeps no offset anyway — stores it.

A value too close to year 1 or 9999 to move to UTC still overflows, and the
API answers that 400 "Value cannot be stored" (`apps/api/api.py`).
"""
from __future__ import annotations

import datetime as dt

import psycopg
from psycopg.types.datetime import DatetimeDumper

#: The widest UTC offset PostgreSQL parses.
_PG_MAX_OFFSET = dt.timedelta(hours=15, minutes=59, seconds=59)


class OffsetSafeDatetimeDumper(DatetimeDumper):
    def dump(self, obj: dt.datetime):
        offset = obj.utcoffset()
        if offset is not None and abs(offset) > _PG_MAX_OFFSET:
            obj = obj.astimezone(dt.UTC)
        return super().dump(obj)


def install() -> None:
    """Register the dumper on psycopg's global adapters, which every new
    connection (Django's included) copies. Call before the first connection."""
    psycopg.adapters.register_dumper(dt.datetime, OffsetSafeDatetimeDumper)
