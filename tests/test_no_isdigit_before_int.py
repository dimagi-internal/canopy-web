"""App code tests digit strings with `str.isdecimal()`, never `str.isdigit()`.

`isdigit()` is True for characters `int()` refuses: superscripts and other
digit-like marks ("³".isdigit() is True; int("³") raises ValueError). Used as
the guard before `int(...)` or a pk lookup, it turns a stray character in a
query string into a 500. `isdecimal()` is exactly the set `int()` accepts.
The nightly contract fuzz found this on GET /api/tasks/count/.

Migrations are historical and left alone.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.guard

APPS = Path(__file__).resolve().parent.parent / "apps"


def test_no_isdigit_in_app_code():
    offenders = [
        f"{path.relative_to(APPS.parent)}:{n}"
        for path in APPS.rglob("*.py")
        if "tests" not in path.parts and "migrations" not in path.parts
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if ".isdigit()" in line
    ]
    assert not offenders, "use .isdecimal(), which matches what int() accepts:\n" + "\n".join(offenders)
