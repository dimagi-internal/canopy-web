"""Decision rows on a storyboard — ace-web's decision-log pattern.

Every default a set of demos is built on is ONE row. Whether it reads as an
assumption, an open ask or a ruling follows from `status` and `review_ask`,
exactly as in ACE `docs/decisions-contract.md` and ace-web's DecisionsReview.

Request-free on purpose: the publish routes, the edit route, the round close
(plan C) and the data migration all call this module, so they cannot drift.
"""
from __future__ import annotations

STATUSES = ("ai-default", "human-decided", "overridden", "deferred")
REVIEW_ASKS = ("recommended-confirmation", "required-before")
NEEDED_BY = ("external-share", "next-run")
EVIDENCE_BASES = ("stated", "inferred", "conflicting")

#: A person ruled on these; a publish may not move their value.
HUMAN_STATUSES = frozenset({"human-decided", "overridden"})
