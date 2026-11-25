"""THE person-only question every brain route asks: is this the person, as themselves?

Consent, removal, the disclosure summary, reporting lines, the manager's reading of outputs and the brain page are
the person's own acts and views. A request is that person only on their browser session or a PAT they minted by
hand, never over MCP, and never what `harness.acting_person.refusal` refuses (an agent's login, a turn's login or
token, a request naming a turn or acting on someone's behalf, a runner). An OAuth client signed in as them (an AI
client, a connector) and a host site's or MCP host grant's delegated token speak FOR them, so they are refused too,
as HCP's governance plane refuses them. `tests/test_brain_person_only.py`."""
from __future__ import annotations

from apps.api.errors import TYPE_FORBIDDEN, ProblemError
from apps.contacts import people
from apps.harness import acting_person
from apps.tokens import kinds

#: The credentials a person uses as themselves: their browser session or a PAT they minted by hand.
PERSON_KINDS = frozenset({kinds.SESSION, kinds.PAT})


def refusal(request) -> str | None:
    """Why `request` is not the person as themselves, or None."""
    if getattr(request, "via_mcp", False):
        return "mcp"
    kind = kinds.of_request(request)
    if kind is None or kind.name not in PERSON_KINDS:
        return "credential"
    if not people.is_human_account(getattr(request, "user", None)):
        return "not_a_person"
    return acting_person.refusal(request)


def is_person(request) -> bool:
    return refusal(request) is None


def the_person(request):
    """The person, as themselves, else a 403 naming nothing about why."""
    if refusal(request) is not None:
        raise ProblemError(403, "Only the person, as themselves, may do this", type_=TYPE_FORBIDDEN)
    return request.user
