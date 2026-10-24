"""Every kind of credential, and what each may do, is one table (`apps/tokens/kinds.py`).

The questions asked of a credential — may it mint another, may it act on someone's
behalf, is it a turn speaking for the person who asked — read that table. These tests
hold the code to it: a comparison against a kind's name written anywhere else fails,
a kind a door records that the table does not know fails, and each question answers
what the table says for every kind.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from django.test import RequestFactory

from apps.harness import turn_asker
from apps.tokens import kinds, principal
from apps.tokens.middleware import MINTING_CREDENTIALS, minting_refusal

ROOT = pathlib.Path(__file__).resolve().parents[1]
KIND_NAMES = frozenset(kinds.KINDS)
#: Where a kind's name may be written as a string: the table, and the doors that
#: record which kind authenticated a request.
RECORDERS = {"apps/tokens/kinds.py"}


def _request(kind_name: str | None):
    request = RequestFactory().get("/")
    if kind_name is not None and kind_name != kinds.SESSION:
        request.auth_credential = {"type": kind_name, "id": 1, "label": "x"}
    return request


def _strings(node) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return set().union(*(_strings(e) for e in node.elts)) if node.elts else set()
    return set()


def _credential_read(node) -> bool:
    """`x.auth_method`, `cred.get("type")`, `cred["type"]`, `claims.get("auth_method")`."""
    text = ast.unparse(node)
    return "auth_method" in text or "'type'" in text or "credential_type" in text


def test_no_check_compares_a_credential_against_a_kind_written_out():
    """`auth_method == "caller_token"` is the shape this replaced: each one a
    separate decision about a kind. Ask the table (`kinds.speaks_for_a_turn`,
    `kinds.kind_of(cred).may_mint`, …) or compare against its constant."""
    found = []
    for path in sorted((ROOT / "apps").rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        if "/tests/" in rel or "/migrations/" in rel or rel in RECORDERS:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Compare):
                continue
            sides = [node.left, *node.comparators]
            literal = set().union(*(_strings(s) for s in sides)) & KIND_NAMES
            if literal and any(_credential_read(s) for s in sides):
                found.append(f"{rel}:{node.lineno} {ast.unparse(node)}")
    assert not found, "Compare against apps/tokens/kinds.py, not a string:\n" + "\n".join(found)


def test_every_kind_the_bearer_door_resolves_is_in_the_table():
    # `principal` names its kinds; a PAT minted for an OAuth grant records `oauth`.
    assert principal.ALL_KINDS <= KIND_NAMES
    assert kinds.OAUTH in KIND_NAMES


@pytest.mark.parametrize("name", sorted(KIND_NAMES))
def test_minting_follows_the_table(name):
    refused = minting_refusal(_request(name))
    assert (refused is None) is kinds.KINDS[name].may_mint, name


def test_a_kind_the_table_does_not_know_may_not_mint():
    assert minting_refusal(_request("something-new")) is not None
    assert MINTING_CREDENTIALS == kinds.names(may_mint=True) - {kinds.SESSION}


@pytest.mark.parametrize("name", sorted(KIND_NAMES))
def test_only_the_table_s_kinds_may_act_on_behalf(name):
    """`X-Canopy-On-Behalf-Of` is refused, before anything else is looked up, for a
    kind the table does not let act on behalf."""
    if kinds.KINDS[name].may_act_on_behalf:
        return  # its own checks (the turn's login) are test_turn_asker's
    with pytest.raises(turn_asker.Refused, match="another credential"):
        turn_asker.resolve(None, {"type": name, "id": 1}, "someone@dimagi.com")


@pytest.mark.parametrize("name", sorted(KIND_NAMES))
def test_a_turn_speaking_for_its_asker_is_told_apart_by_the_table(name):
    assert kinds.speaks_for_a_turn(_request(name)) is kinds.KINDS[name].speaks_for_a_turn


def test_an_unknown_kind_is_never_taken_for_the_person():
    assert kinds.speaks_for_a_turn(_request("something-new")) is True
