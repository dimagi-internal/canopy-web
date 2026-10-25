"""`select_for_update()` beside `select_related` must lock only its own row.

`select_related` on a NULLABLE foreign key is a LEFT OUTER JOIN, and Postgres refuses
`FOR UPDATE` on the nullable side of one: "FOR UPDATE cannot be applied to the
nullable side of an outer join". SQLite ignores FOR UPDATE, so the suite stays green
while production 500s. That is how every canopy-pat rotation broke —
`Agent.objects.select_for_update().select_related("user", ...)`, `Agent.user` nullable —
found only when an agent tried to rotate a leaked token. `of=("self",)` locks the row
being changed, which is what every such call means.
"""
from __future__ import annotations

import re

from tests._source import iter_app_sources

BARE = re.compile(r"select_for_update\(\)\s*\.\s*select_related")


def test_select_for_update_with_select_related_locks_only_self():
    offenders = [f"{s.rel}:{s.text[:m.start()].count(chr(10)) + 1}"
                 for s in iter_app_sources() for m in BARE.finditer(s.text)]
    assert not offenders, (
        f"{offenders}: use select_for_update(of=(\"self\",)) — a bare FOR UPDATE with "
        "select_related fails on Postgres when the related FK is nullable.")
