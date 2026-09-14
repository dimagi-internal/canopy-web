"""An agent row is an INSTANCE; its definition is the repo it points at.

This is the model that replaced a designed-but-unbuilt `AgentInstance` table.
The codebase settled it: `AgentSkill`'s own docstring says the catalog "is
replaced wholesale on each publish (PUT) so it always mirrors the repo", and
that model was the ONE the old design put on the definition side — using it as
its own sanity check ("if more than one or two models want to live on the
definition, the boundary is drawn wrong"). Zero do. The definition was never a
database concept.

What these tests protect is the one derived fact that model needs: whether two
instances are running the SAME definition. Everything else already worked —
`Agent.workspace` has been NOT NULL since `agents/0013`, which is to say the
instance model shipped a year before it was named.
"""
from __future__ import annotations

import pytest

from apps.agents.definition import definition_key, siblings
from apps.agents.models import Agent
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("url", [
    "https://github.com/dimagi-internal/ace",
    "https://github.com/dimagi-internal/ace.git",
    "https://github.com/dimagi-internal/ace/",
    "git@github.com:dimagi-internal/ace.git",
    "ssh://git@github.com/dimagi-internal/ace.git",
    "HTTPS://GitHub.com/Dimagi-Internal/ace",
])
def test_every_spelling_of_one_repo_is_one_definition(url):
    """A normalisation miss means "these agents are unrelated", silently.

    This key is the ONLY thing tying two instances together, so an equality
    test on the raw column would report three spellings of one repo as three
    definitions — and the symptom is not an error, it is a fix that appears
    not to have reached a tenant it did reach.
    """
    assert definition_key(url) == "github.com/dimagi-internal/ace"


def test_a_non_default_ref_is_a_different_definition():
    """An instance pinned to `staging` is not running what `main` runs.

    Treating them as identical would report a fix as having landed somewhere it
    has not. `main` and `master` and blank all mean "the default", so they must
    NOT split one definition into several.
    """
    base = definition_key("https://github.com/dimagi-internal/ace")
    assert definition_key("https://github.com/dimagi-internal/ace", "main") == base
    assert definition_key("https://github.com/dimagi-internal/ace", "master") == base
    assert definition_key("https://github.com/dimagi-internal/ace", "") == base
    assert definition_key("https://github.com/dimagi-internal/ace", "staging") != base


def test_no_repo_is_not_a_definition():
    """An agent canopy cannot see the definition of. Returning "" and never
    matching is right: the alternative — a `repo_url=""` filter — would report
    every repoless agent as a sibling of every other."""
    assert definition_key("") == ""
    assert definition_key("   ") == ""


def test_two_tenants_running_the_same_repo_are_siblings():
    """The payoff of the whole model, made checkable.

    "Improving echo improves it everywhere" is only true if the instances
    really do share a definition, and this is how you ask.
    """
    a = a_workspace("tenant-a")
    b = a_workspace("tenant-b")
    one = Agent.objects.create(
        slug="ace", name="ACE", workspace=a,
        repo_url="https://github.com/dimagi-internal/ace")
    two = Agent.objects.create(
        slug="ace-b", name="ACE", workspace=b,
        repo_url="git@github.com:dimagi-internal/ace.git")
    unrelated = Agent.objects.create(
        slug="echo", name="Echo", workspace=a,
        repo_url="https://github.com/dimagi-internal/echo")

    found = list(siblings(one))
    assert found == [two]
    assert unrelated not in found
    # Symmetric, which it must be for "who else runs this" to mean anything.
    assert list(siblings(two)) == [one]


def test_siblings_never_includes_the_agent_itself():
    ws = a_workspace("tenant-a")
    only = Agent.objects.create(
        slug="ace", name="ACE", workspace=ws,
        repo_url="https://github.com/dimagi-internal/ace")
    assert list(siblings(only)) == []


def test_repoless_agents_are_not_siblings_of_each_other():
    """The naive implementation's bug: `filter(repo_url=agent.repo_url)` makes
    every agent with no repo a sibling of every other, which would report an
    unrelated fleet as sharing a definition."""
    ws = a_workspace("tenant-a")
    one = Agent.objects.create(slug="a1", name="A1", workspace=ws)
    Agent.objects.create(slug="a2", name="A2", workspace=ws)
    assert list(siblings(one)) == []


def test_a_fork_is_a_different_definition():
    """Forking is pointing somewhere else — there is no "detached instance"
    state to represent, and no merge story to design."""
    ws = a_workspace("tenant-a")
    original = Agent.objects.create(
        slug="ace", name="ACE", workspace=ws,
        repo_url="https://github.com/dimagi-internal/ace")
    Agent.objects.create(
        slug="ace-fork", name="ACE fork", workspace=ws,
        repo_url="https://github.com/someone-else/ace")
    assert list(siblings(original)) == []


def test_the_instance_model_is_already_enforced():
    """`Agent.workspace` is NOT NULL, which is what makes the row an instance.

    Pinned because the abandoned `AgentInstance` migration would have made
    tenancy nullable during the transition — reintroducing across ~70 call
    sites the `workspace_id IS NULL` means ALLOW surface that this constraint
    exists to eliminate. Nothing about the repo-as-definition model needs that,
    and this asserts the property stays.
    """
    assert Agent._meta.get_field("workspace").null is False
