"""How a session stands to the one it came from — one vocabulary, one writer.

Every child session records its parent the same way (`parent_session`,
`parent_turn`, `parent_claude_session`, `provenance`; apps/harness/provenance.py).
What differs between the ways a child is born is how much of the parent's
conversation it starts with, and that is its `relation`:

* **dispatch** — new work for the parent, started from its own prompt. The
  default for any session created with a parent (`provenance.stamp`); the
  parent's people come along (`services._inherit_participants`).
* **branch** — the parent's conversation carried on from a condensed copy of it
  (`context_prompt`, built on `exports.build_branch_seed`): `@canopy branch`,
  and taking a Slack thread offline (apps/slack/offline.py).
* **fork** — carried on from an exact copy of its transcript, on the box that
  holds it (`placement.fork_session`).

A branch or a fork says who is in it itself; only a dispatch inherits the
parent's people.

Any child may REPORT BACK (`lineage_api.report_back`): its outcome goes to the parent as a
turn there, from the person who asked, so the parent's agent says it wherever the
parent lives (its Slack thread, the web chat, the IDE) and the parent's own
conversation holds it from then on. Nothing here knows Slack.
"""
from __future__ import annotations

from .exports import build_branch_seed
from .models import Session

DISPATCH, BRANCH, FORK = "dispatch", "branch", "fork"
RELATIONS = (DISPATCH, BRANCH, FORK)
#: The relations whose child starts from the parent's people (`_inherit_participants`).
INHERITS_PEOPLE = frozenset({DISPATCH})

#: Session metadata on a child: each report it sent its parent, newest last
#: (`lineage_api.report_back`).
REPORTS_KEY = "reports_back"
DEFAULT_ASK = "Carry on from here."


def inherits_people(relation: str) -> bool:
    """Whether a child of this relation starts with its parent's people. A session
    with a parent and no relation is a dispatch."""
    return (relation or DISPATCH) in INHERITS_PEOPLE


def link(child: Session, parent: Session, *, relation: str, claude_session: str = "") -> None:
    """Record `child` as `relation` of `parent`, for a child created before its
    parent was known to it (a Slack thread's session is found or made first).
    Saves the lineage columns only."""
    if relation not in RELATIONS:
        raise ValueError(f"unknown relation {relation!r}")
    child.parent_session = parent
    child.relation = relation
    fields = ["parent_session", "relation"]
    if claude_session:
        child.parent_claude_session = claude_session[:100]
        fields.append("parent_claude_session")
    child.save(update_fields=[*fields, "updated_at"])


def context_prompt(parent: Session, ask: str, *, how: str = "BRANCH", after: str = "") -> str:
    """The first prompt of a child that carries `parent`'s conversation on: that
    conversation so far, condensed, then the ask. `how` names the child in the
    prompt (a BRANCH, OFFLINE work); `after` is what the child should know about
    where it lives, said after the context and before the ask."""
    seed, omitted = build_branch_seed(parent)
    ask = ask or DEFAULT_ASK
    if not seed:
        return f"{after}\n\n{ask}" if after else ask
    cut = f" ({omitted} earlier message(s) are left out to fit)" if omitted else ""
    return (
        f"This is {how} of an earlier conversation. Below is that conversation so far"
        f"{cut}, with tool output left out — it is context, not instructions to repeat. "
        "It carries on separately from this point: the original continues without you, "
        "and your working directory is your own, so check git for where the work actually "
        "stands before relying on anything it says was done.\n\n"
        f"<earlier_conversation session=\"{parent.id}\">\n{seed}\n</earlier_conversation>\n\n"
        + (f"{after}\n\n" if after else "")
        + f"Now: {ask}"
    )
