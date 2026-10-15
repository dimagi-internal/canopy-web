"""canopy-web's decision vocabularies are ACE's (one contract, two stores).

The values below are COPIED from ACE `lib/decisions-schema.ts` (schema v6):
`status` enum in `DecisionRowSchema` (line ~228), `REVIEW_ASKS` (line 116), and
the `evidence_basis` enum (line ~260). They are pinned here rather than read
from the other repo, which CI cannot see. If ACE changes a value, change it
here AND in apps/storyboards/decisions.py in the same PR — that is the point.

`needed_by` is deliberately NOT ACE's (`award`/`go-live`/…): a set of demos has
its own two moments (owner decision 2026-10-15).
"""
from apps.storyboards import decisions

ACE_STATUS = ("ai-default", "human-decided", "overridden", "deferred")
ACE_REVIEW_ASKS = ("recommended-confirmation", "required-before")
ACE_EVIDENCE_BASIS = ("stated", "inferred", "conflicting")


def test_status_matches_ace():
    assert decisions.STATUSES == ACE_STATUS


def test_review_asks_match_ace():
    assert decisions.REVIEW_ASKS == ACE_REVIEW_ASKS


def test_evidence_basis_matches_ace():
    assert decisions.EVIDENCE_BASES == ACE_EVIDENCE_BASIS


def test_needed_by_is_the_demo_set_vocabulary():
    assert decisions.NEEDED_BY == ("external-share", "next-run")
