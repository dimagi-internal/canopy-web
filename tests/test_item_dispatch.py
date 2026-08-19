"""dispatch() — an approved Item becomes work. Self by default; anyone on request."""
from __future__ import annotations

import pytest

from apps.agents.models import Agent
from apps.harness.dispatch import TurnSpec, dispatch
from apps.harness.models import Item, Turn
from apps.workspaces import services as wsvc

pytestmark = pytest.mark.django_db


@pytest.fixture
def ws(default_workspace):
    return wsvc.ensure_default_workspace()


@pytest.fixture
def ada(ws):
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws)


@pytest.fixture
def hal(ws):
    return Agent.objects.create(slug="hal", name="Hal", workspace=ws)


def _item(agent, **kw):
    kw.setdefault("idempotency_key", f"k-{agent.slug}-{kw.get('title', 'x')}")
    kw.setdefault("kind", Item.REVIEW)
    kw.setdefault("title", "x")
    kw.setdefault("origin", Turn.ORIGIN_API)
    return Item.objects.create(agent=agent, **kw)


def test_empty_target_agent_dispatches_to_the_items_own_agent(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])

    turns = dispatch(item, actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert [t.agent for t in turns] == [ada]
    # Stamped now (the brief came off an agent's card), so the prompt carries a provenance
    # footer. The ASK is still first and still verbatim — that is the part callers depend on.
    assert turns[0].prompt.startswith("/ada:conduct")
    assert turns[0].raised_from == item


def test_named_target_agent_dispatches_to_that_agent(ada, hal):
    item = _item(ada, dispatch=[{"target_agent": "hal", "prompt": "/hal:turn", "origin": "email"}])

    turns = dispatch(item, actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert [t.agent for t in turns] == [hal]
    assert turns[0].origin == "email"


def test_dispatch_is_idempotent_per_entry(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])

    first = dispatch(item, actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})
    second = dispatch(item, actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert [t.id for t in first] == [t.id for t in second]
    assert Turn.objects.count() == 1


def test_an_item_with_no_dispatch_enqueues_nothing(ada):
    item = _item(ada, dispatch=[])

    assert dispatch(item, actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG}) == []
    assert Turn.objects.count() == 0


def test_an_unknown_target_agent_raises_rather_than_silently_dropping(ada):
    item = _item(ada, dispatch=[{"target_agent": "ghost", "prompt": "/ghost:turn"}])

    with pytest.raises(ValueError, match="ghost"):
        dispatch(item, actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})


def test_each_entry_gets_its_own_turn(ada, hal):
    item = _item(ada, dispatch=[
        {"target_agent": "hal", "prompt": "/hal:turn"},
        {"prompt": "/ada:conduct"},
    ])

    turns = dispatch(item, actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert [t.agent for t in turns] == [hal, ada]


def test_a_spec_with_no_prompt_falls_back_to_the_targets_turn(ada, hal):
    item = _item(ada, dispatch=[{"target_agent": "hal"}])

    turns = dispatch(item, actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    # Stamped, like every other brief this path enqueues — a board drain is machine-authored
    # too. The fallback ask itself is unchanged and still leads.
    assert turns[0].prompt.startswith("/hal:turn")


def test_turnspec_defaults_target_self():
    assert TurnSpec(prompt="/x").target_agent == ""


def test_cross_workspace_dispatch_is_refused_for_a_non_member(ada):
    # hal lives in "connect"; an actor who is NOT a member of connect must not be
    # able to land a prompt on hal by dispatching from an item on their own agent.
    from django.contrib.auth import get_user_model
    from apps.workspaces.models import Workspace

    owner = get_user_model().objects.create(username="o@connect.example", email="o@connect.example")
    connect = Workspace.objects.create(
        slug="connect", display_name="Connect", created_by=owner, auto_join_domains=[]
    )
    hal_connect = Agent.objects.create(slug="hal", name="Hal", workspace=connect)
    item = _item(ada, dispatch=[{"target_agent": "hal", "prompt": "/hal:turn"}])

    # Actor in "dimagi" only → cross-tenant dispatch to hal (connect) is refused.
    with pytest.raises(ValueError, match="not a member"):
        dispatch(item, actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})
    assert Turn.objects.count() == 0  # nothing landed on hal

    # An actor who IS a member of connect (e.g. Jonathan, in both) can dispatch.
    turns = dispatch(item, actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG, "connect"})
    assert [t.agent for t in turns] == [hal_connect]


# ── provenance: the enqueued brief says a machine wrote it, and whose ────────────────────────
# This is the ONE enqueue path where the server knows the prompt was written by an agent — it
# came off the agent's own card. `services.enqueue_turn` deliberately does NOT stamp: its other
# callers include canopy_sessions, where the prompt is a human typing in the web chat, and
# blanket-stamping would suppress the human's own messages from the corrections lens — a
# strictly worse bug than the one being fixed.

def test_the_enqueued_brief_is_stamped_as_machine_authored(ada, hal):
    from apps.harness.dispatch_marker import DISPATCH_MARKER

    item = _item(ada, dispatch=[{"target_agent": "hal", "prompt": "FINDING: x"}])
    turn = dispatch(item, actor_workspace_slugs={ada.workspace_id})[0]
    assert DISPATCH_MARKER in turn.prompt
    assert turn.prompt.startswith("FINDING: x"), "the ask must still come first"


def test_the_stamp_names_the_card_owner_as_the_sender(ada, hal):
    """The verdict on a dispatched session gets handed to whoever sent the brief, so an
    unattributed brief is an ungraded one. The sender is the CARD'S agent, not the target."""
    from apps.harness.dispatch_marker import SENDER_MARKER

    item = _item(ada, dispatch=[{"target_agent": "hal", "prompt": "FINDING: x"}])
    turn = dispatch(item, actor_workspace_slugs={ada.workspace_id})[0]
    assert SENDER_MARKER.format(slug="ada") in turn.prompt


def test_an_already_stamped_prompt_is_not_double_marked(ada, hal):
    """Ada stamps client-side before canopy-web ever sees it (ada#55)."""
    from apps.harness.dispatch_marker import DISPATCH_MARKER, stamp_dispatched

    pre = stamp_dispatched("FINDING: x", sender="ada")
    item = _item(ada, dispatch=[{"target_agent": "hal", "prompt": pre}])
    turn = dispatch(item, actor_workspace_slugs={ada.workspace_id})[0]
    assert turn.prompt.count(DISPATCH_MARKER) == 1


def test_a_board_drain_fallback_is_stamped_too(ada, hal):
    """`/hal:turn` with no brief is still machine-authored."""
    from apps.harness.dispatch_marker import DISPATCH_MARKER

    item = _item(ada, dispatch=[{"target_agent": "hal"}])
    turn = dispatch(item, actor_workspace_slugs={ada.workspace_id})[0]
    assert DISPATCH_MARKER in turn.prompt
    assert turn.prompt.startswith("/hal:turn")


# ── the decider's reply survives the stamp ───────────────────────────────────────────────────

def test_the_deciders_reply_is_delimited(ada, hal):
    """The marker is all-or-nothing per turn, so without delimiters, stamping the brief threw
    the human's answer away with it — and that answer is the highest-value human signal on the
    board: a person overruling, narrowing, or redirecting an agent's proposal."""
    from apps.harness.dispatch_marker import HUMAN_REPLY_CLOSE, HUMAN_REPLY_OPEN

    said = "No, send it to eva instead."
    item = _item(ada, comment=said, decided_by="jonathan",
                 dispatch=[{"target_agent": "hal", "prompt": "FINDING: x"}])
    turn = dispatch(item, actor_workspace_slugs={ada.workspace_id})[0]
    assert f"{HUMAN_REPLY_OPEN}{said}{HUMAN_REPLY_CLOSE}" in turn.prompt


def test_the_reply_still_reaches_the_agent_unchanged(ada, hal):
    """Delimiters are inert HTML comments — the agent doing the work must still read the steer
    exactly as before, and still be told the reply overrides the brief."""
    said = "only the retry, skip the backoff"
    item = _item(ada, comment=said, decided_by="jonathan",
                 dispatch=[{"target_agent": "hal", "prompt": "FINDING: x"}])
    turn = dispatch(item, actor_workspace_slugs={ada.workspace_id})[0]
    assert said in turn.prompt
    assert "ANSWERED BY jonathan" in turn.prompt
    assert "OVERRIDES the brief" in turn.prompt


def test_no_reply_means_no_delimiters(ada, hal):
    from apps.harness.dispatch_marker import HUMAN_REPLY_OPEN

    item = _item(ada, dispatch=[{"target_agent": "hal", "prompt": "FINDING: x"}])
    turn = dispatch(item, actor_workspace_slugs={ada.workspace_id})[0]
    assert HUMAN_REPLY_OPEN not in turn.prompt
