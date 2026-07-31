"""Item state machine: open -> decided (dispatching) | dismissed. One way only."""
from __future__ import annotations

import pytest

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Item, Turn
from apps.workspaces import services as wsvc

pytestmark = pytest.mark.django_db


@pytest.fixture
def ada(default_workspace):
    ws = wsvc.ensure_default_workspace()
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws)


def _item(ada, **kw):
    kw.setdefault("idempotency_key", "k1")
    kw.setdefault("kind", Item.REVIEW)
    kw.setdefault("title", "x")
    kw.setdefault("origin", Turn.ORIGIN_API)
    return Item.objects.create(agent=ada, **kw)


def test_implement_decides_and_dispatches(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])

    item, turns = services.decide_item(item, decision=Item.IMPLEMENT, comment="", by="jj@dimagi.com", actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert item.state == Item.DECIDED
    assert item.decision == Item.IMPLEMENT
    assert item.decided_by == "jj@dimagi.com"
    assert item.decided_at is not None
    assert item.dispatched_at is not None
    assert len(turns) == 1


def test_skip_decides_and_dispatches_nothing(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])

    item, turns = services.decide_item(item, decision=Item.SKIP, comment="", by="jj@dimagi.com", actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert item.state == Item.DECIDED
    assert turns == []
    assert Turn.objects.count() == 0
    assert item.dispatched_at is None


def test_defer_decides_and_dispatches_nothing(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])

    item, turns = services.decide_item(item, decision=Item.DEFER, comment="", by="jj@dimagi.com", actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert item.decision == Item.DEFER
    assert Turn.objects.count() == 0


def test_deciding_twice_raises_rather_than_dispatching_again(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])
    services.decide_item(item, decision=Item.IMPLEMENT, comment="", by="jj@dimagi.com", actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    with pytest.raises(services.AlreadyDecidedError):
        services.decide_item(item, decision=Item.IMPLEMENT, comment="", by="jj@dimagi.com", actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert Turn.objects.count() == 1


def test_a_question_requires_an_answer(ada):
    item = _item(ada, kind=Item.QUESTION, title="which repo?")

    with pytest.raises(ValueError, match="answer"):
        services.decide_item(item, decision="", comment="", by="jj@dimagi.com", actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    item, _ = services.decide_item(item, decision="", comment="canopy-web", by="jj@dimagi.com", actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})
    assert item.state == Item.DECIDED
    assert item.comment == "canopy-web"


def test_answering_a_question_dispatches_its_work(ada):
    """An answer IS the go-ahead. A question card carries the same routing block a
    review does, so answering it enqueues that work immediately — the alternative is
    an answered card sitting inert until an agent happens to re-read decided items."""
    item = _item(ada, kind=Item.QUESTION, title="add a weekday schedule for hal?",
                 dispatch=[{"prompt": "/ada:turn — add hal's schedule"}])

    item, turns = services.decide_item(
        item, decision="", comment="Yeah let's do that", by="jj@dimagi.com",
        actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert item.state == Item.DECIDED
    assert item.decision == ""  # a question is still resolved by its answer, not a verb
    assert item.dispatched_at is not None
    assert len(turns) == 1


def test_an_answer_with_nothing_to_route_is_only_recorded(ada):
    item = _item(ada, kind=Item.QUESTION, title="which repo?")

    item, turns = services.decide_item(
        item, decision="", comment="canopy-web", by="jj@dimagi.com",
        actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert item.state == Item.DECIDED
    assert turns == []
    assert item.dispatched_at is None


def test_the_answer_rides_along_into_the_dispatched_prompt(ada):
    """The answer often redirects the work ('yes, but send it to hal instead'). If the
    agent never sees it, it executes the original brief and the steer is lost."""
    item = _item(ada, kind=Item.QUESTION, title="investigate ace's closes?",
                 dispatch=[{"prompt": "/ace:turn — investigate"}])

    _item_, turns = services.decide_item(
        item, decision="", comment="Yes, but dispatch it to hal", by="jj@dimagi.com",
        actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert "Yes, but dispatch it to hal" in turns[0].prompt
    assert "jj@dimagi.com" in turns[0].prompt


def test_an_implement_comment_rides_along_too(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])

    _item_, turns = services.decide_item(
        item, decision=Item.IMPLEMENT, comment="only the retry, skip the backoff",
        by="jj@dimagi.com", actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert "only the retry, skip the backoff" in turns[0].prompt


def test_a_dispatch_free_decision_leaves_the_prompt_alone(ada):
    """No answer, no preamble — an unadorned brief must stay byte-for-byte itself."""
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])

    _item_, turns = services.decide_item(
        item, decision=Item.IMPLEMENT, comment="", by="jj@dimagi.com",
        actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    assert turns[0].prompt == "/ada:conduct"


def test_a_failing_dispatch_rolls_an_ANSWER_back_too(ada):
    """Same all-or-nothing guarantee the review path has: a bad spec 422s and the
    question stays OPEN and re-answerable, rather than decided-but-inert."""
    item = _item(ada, kind=Item.QUESTION, title="?",
                 dispatch=[{"target_agent": "ghost", "prompt": "/ghost:turn"}])

    with pytest.raises(ValueError, match="ghost"):
        services.decide_item(item, decision="", comment="go", by="jj@dimagi.com",
                             actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    item.refresh_from_db()
    assert item.state == Item.OPEN
    assert Turn.objects.count() == 0


def test_a_review_rejects_a_decision_outside_the_closed_set(ada):
    item = _item(ada)

    with pytest.raises(ValueError, match="decision"):
        services.decide_item(item, decision="yolo", comment="", by="jj@dimagi.com", actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})


def test_a_failing_dispatch_rolls_the_decision_back(ada):
    """A bad spec must NOT leave a decided-but-undispatched item. Deciding twice is
    409, so committing the decision before dispatch would strand the item forever:
    approved in the UI, work never enqueued, unfixable. It stays OPEN and retryable."""
    item = _item(ada, dispatch=[{"target_agent": "ghost", "prompt": "/ghost:turn"}])

    with pytest.raises(ValueError, match="ghost"):
        services.decide_item(item, decision=Item.IMPLEMENT, comment="", by="jj@dimagi.com", actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    item.refresh_from_db()
    assert item.state == Item.OPEN
    assert item.decided_at is None
    assert Turn.objects.count() == 0


def test_a_partly_bad_dispatch_enqueues_nothing(ada):
    """All-or-nothing: entry 0 must not survive entry 1 failing."""
    item = _item(ada, dispatch=[
        {"prompt": "/ada:conduct"},
        {"target_agent": "ghost", "prompt": "/ghost:turn"},
    ])

    with pytest.raises(ValueError):
        services.decide_item(item, decision=Item.IMPLEMENT, comment="", by="jj@dimagi.com", actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG})

    item.refresh_from_db()
    assert item.state == Item.OPEN
    assert Turn.objects.count() == 0


def test_dismiss_never_dispatches_even_with_a_decision_set(ada):
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}], decision=Item.IMPLEMENT)

    item = services.dismiss_item(item, by="jj@dimagi.com")

    assert item.state == Item.DISMISSED
    assert Turn.objects.count() == 0


def test_dismiss_records_an_optional_reason(ada):
    # A producer retracting its own erroneous item records WHY; the reason lands on
    # the item's comment so the board shows it instead of a bare dismissed row.
    item = _item(ada)
    item = services.dismiss_item(item, by="ada@dimagi-ai.com",
                                 comment="retracted: already shipped in origin/main")
    assert item.state == Item.DISMISSED
    assert item.comment == "retracted: already shipped in origin/main"


def test_dismiss_reason_is_optional(ada):
    # Backward-compatible: an empty-body dismiss (no comment) leaves comment untouched.
    item = _item(ada)
    item = services.dismiss_item(item, by="jj@dimagi.com")
    assert item.state == Item.DISMISSED
    assert item.comment == ""


def test_dismiss_refuses_an_already_decided_item(ada):
    # Dismissing a decided-and-dispatched item would erase who approved it while its
    # turns keep running. Dismiss guards on state exactly like decide does.
    item = _item(ada, dispatch=[{"prompt": "/ada:conduct"}])
    item, _turns = services.decide_item(
        item, decision=Item.IMPLEMENT, comment="", by="approver@dimagi.com", actor_workspace_slugs={wsvc.DEFAULT_WORKSPACE_SLUG}
    )
    assert item.state == Item.DECIDED

    with pytest.raises(services.AlreadyDecidedError):
        services.dismiss_item(item, by="dismisser@dimagi.com")

    item.refresh_from_db()
    assert item.state == Item.DECIDED  # unchanged
    assert item.decided_by == "approver@dimagi.com"  # approval record intact
    assert item.dispatched_at is not None  # the dispatched turn still stands


def test_dismiss_twice_is_a_conflict_not_a_silent_rewrite(ada):
    item = _item(ada)
    services.dismiss_item(item, by="first@dimagi.com")

    with pytest.raises(services.AlreadyDecidedError):
        services.dismiss_item(item, by="second@dimagi.com")

    item.refresh_from_db()
    assert item.decided_by == "first@dimagi.com"


def test_create_items_is_idempotent_per_key(ada):
    payload = [{"kind": "review", "title": "a", "origin": "audit", "idempotency_key": "dupe"}]

    first = services.create_items(agent=ada, payloads=payload)
    second = services.create_items(agent=ada, payloads=payload)

    assert [i.id for i in first] == [i.id for i in second]
    assert Item.objects.count() == 1
