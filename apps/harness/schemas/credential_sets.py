"""Runner credential set schemas."""
from __future__ import annotations

import uuid

from ninja import Schema
from pydantic import Field


class RunnerCredentialSetRunnerOut(Schema):
    id: uuid.UUID
    name: str
    workspace: str | None = None


class RunnerCredentialSetVaultOut(Schema):
    """The set's vault for one workspace tree: its values for the runners in that tree."""

    vault_id: str
    name: str
    workspace: str
    #: The root of the tree it serves.
    root: str
    #: Its key version; 0 = no key yet.
    kv: int = 0
    #: The list's entries (`vault_secret`) with no value in this vault yet.
    missing: list[str] = []


class RunnerCredentialSetEntryOut(Schema):
    """One entry of the set's list, in order."""

    name: str = ""
    #: login / token / api_key
    kind: str


class RunnerCredentialSetCandidateOut(Schema):
    """One of the owner's other cloud runners, as a runner the set could be given to."""

    id: uuid.UUID
    name: str
    workspace: str | None = None
    root: str = ""
    #: In a tree the set has no vault for yet: its values are then sealed there anew.
    new_tree: bool = False
    #: Why it cannot read the set now; null when it can.
    refusal: str | None = None


class RunnerCredentialSetOut(Schema):
    """A credential set: one ordered list of Claude credentials that every runner on
    it reads, with a vault per workspace tree for the values."""

    id: uuid.UUID
    name: str
    workspace: str
    entries: list[RunnerCredentialSetEntryOut] = []
    runners: list[RunnerCredentialSetRunnerOut] = []
    #: The owner's other cloud runners, for adding one to the set.
    candidates: list[RunnerCredentialSetCandidateOut] = []
    vaults: list[RunnerCredentialSetVaultOut] = []


class RunnerCredentialSetIn(Schema):
    """Share a cloud runner's own credential list as a new set."""

    runner: uuid.UUID
    name: str = Field(..., min_length=1, max_length=100)


class RunnerCredentialSetChoiceIn(Schema):
    """Which set a runner reads: a set's id, or null for its own list again."""

    credential_set: uuid.UUID | None = None
