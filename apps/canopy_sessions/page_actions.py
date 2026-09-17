"""Calling into the page a user is looking at.

An embedded host declares what its page can do (`registerAction` in
`canopy-widget`); the agent driving that session calls one. This module is the
middle: it validates the call against the host's own declaration, writes the
durable record, rings the doorbell, and waits — briefly — for the page to
answer.

**The shape is ACP's, not MCP's.** MCP tools here are static Python functions,
declared at build time and scoped to a connection. Page actions are declared by
the host at runtime, scoped to one session, and valid only while a tab is open —
the opposite on every axis. ACP already models exactly this: a client advertises
capabilities and the agent invokes them over the session. canopy takes the SHAPE
rather than the wire format, the same move the ACP spec made for the laptop.

**Nothing here queues.** `RunnerBinding.pending_answer` is drained by a runner
that comes back; a browser tab may never return. An action that outlived its
page would fire into a different screen, or resolve an agent's request minutes
after it stopped being true. So an unanswered action EXPIRES and the agent is
told the page was not open — which is honest, and is the one answer it can act
on.
"""

from __future__ import annotations

import time

from django.utils import timezone

from apps.realtime.groups import publish, session_group

from .models import PageAction, Session

#: How long the caller waits for the page to answer. Long enough for a human's
#: browser to execute a callback and post back over an open socket; short enough
#: that an agent is not left hanging on a tab that has gone. The page either
#: answers in well under this or is not there.
DEFAULT_TIMEOUT_SECONDS = 20

#: How often the waiter re-reads the row. The answer arrives by HTTP POST from
#: the page, so there is nothing to await directly — polling one indexed row is
#: the cheap, correct way to notice.
POLL_INTERVAL_SECONDS = 0.25


class PageActionError(Exception):
    """Raised for every refusal, each carrying a reason the agent can read.

    `code` is a closed set the caller can branch on without parsing prose:
    `no_page` (nothing attached), `unknown_action` (the page does not offer it),
    `bad_arguments` (failed the host's own schema), `timeout` (the page never
    answered), `refused` (the host's callback said no).
    """

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def declared_actions(session: Session) -> list[dict]:
    """What the attached page currently says it can do."""
    return list(session.page_actions_available or [])


def set_declared_actions(session: Session, actions: list[dict]) -> None:
    """Replace the declaration wholesale.

    Never merged: a page has ONE current set of capabilities, and an action left
    over from the page a user navigated away from is one the agent would call
    into nothing.
    """
    cleaned: list[dict] = []
    for a in actions or []:
        if not isinstance(a, dict) or not a.get("name"):
            continue
        cleaned.append({
            "name": str(a["name"]),
            "description": str(a.get("description", "")),
            # JSON-Schema, passed through as the host wrote it. canopy does not
            # interpret it beyond the shallow required/type check below — the
            # host is the only party that knows what its own action means.
            #
            # Stored under ONE name whichever the host used. Reading only
            # `parameters` here discarded an `inputSchema` declaration outright,
            # which is worse than not supporting it: the tool still published,
            # with no schema, and a missing required argument then waited out
            # the full timeout and came back as "the page is probably closed".
            "parameters": schema_of(a),
        })
    session.page_actions_available = cleaned
    session.save(update_fields=["page_actions_available"])


def schema_of(spec: dict) -> dict:
    """The argument schema off a declaration, under either spelling.

    `inputSchema` is MCP's name for it and `parameters` is OpenAI
    function-calling's; `apps/mcp/page_tools.py` publishes a tool from either,
    so validation has to read either too. It did not — it looked only at
    `parameters`, which meant a host writing MCP's own spelling got a tool with
    a schema the agent could see and the server never enforced. The visible
    cost was the wrong error: a missing required argument waited out the full
    timeout and came back as "the page is probably closed", which is both
    untrue and unactionable.
    """
    return spec.get("parameters") or spec.get("inputSchema") or {}


def _validate(spec: dict, args: dict) -> None:
    """A shallow check against the host's declared schema.

    Deliberately not a full JSON-Schema validator: the host's callback is the
    real authority and can refuse anything it dislikes. This catches the case
    that is purely a waste of a round trip — a missing required argument — and
    the one that reads as a page bug rather than an agent one, an argument of
    the wrong primitive type.
    """
    params = schema_of(spec)
    props = params.get("properties") or {}
    for field in params.get("required") or []:
        if field not in args:
            raise PageActionError(
                "bad_arguments", f"{spec['name']} requires {field!r}, which was not supplied"
            )
    types = {"string": str, "number": (int, float), "integer": int,
             "boolean": bool, "array": list, "object": dict}
    for key, value in args.items():
        declared = (props.get(key) or {}).get("type")
        expected = types.get(declared) if declared else None
        # `bool` is a subclass of `int`, so an explicit integer field would
        # silently accept True without this.
        if expected and (not isinstance(value, expected)
                         or (declared in ("number", "integer") and isinstance(value, bool))):
            raise PageActionError(
                "bad_arguments", f"{spec['name']}.{key} should be a {declared}"
            )


def request_action(
    *, session: Session, name: str, args: dict, user,
    timeout: float | None = None,
) -> PageAction:
    """Ask the attached page to run one action, and wait for its answer.

    Raises `PageActionError` for every outcome that is not a completed action,
    so a caller cannot mistake a refusal or an absent page for success.

    `timeout` defaults to `DEFAULT_TIMEOUT_SECONDS` READ AT CALL TIME. It was a
    default argument, which binds once at import — so the module constant that
    every caller and test treats as the knob was not one, and the tests that
    set it to shrink a 20-second wait were quietly waiting the full 20.
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT_SECONDS
    available = {a["name"]: a for a in declared_actions(session)}
    if not available:
        raise PageActionError(
            "no_page",
            "no page is attached to this session, so there is nothing to act on. "
            "Actions run in the user's open browser tab and cannot be queued.",
        )
    spec = available.get(name)
    if spec is None:
        raise PageActionError(
            "unknown_action",
            f"the attached page does not offer {name!r}. It offers: "
            f"{', '.join(sorted(available)) or 'nothing'}",
        )
    _validate(spec, args or {})

    action = PageAction.objects.create(
        session=session, name=name, args=args or {}, requested_for=user
    )

    # The frame, AFTER the row exists — so a page that receives the doorbell can
    # always find the record, and a page that never receives it leaves a row to
    # expire rather than a request that never existed.
    publish(session_group(session.id), {
        "type": "session.page_action",
        "action": {"id": str(action.id), "name": name, "args": args or {}},
    })

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        action.refresh_from_db()
        if action.status == PageAction.DONE:
            return action
        if action.status == PageAction.FAILED:
            raise PageActionError("refused", action.error or "the page refused the action")

    PageAction.objects.filter(pk=action.pk, status=PageAction.PENDING).update(
        status=PageAction.EXPIRED, resolved_at=timezone.now()
    )
    raise PageActionError(
        "timeout",
        f"the page did not answer within {timeout:g}s. It is probably closed — "
        "an action only runs while the user has the page open.",
    )


def resolve(action: PageAction, *, result=None, error: str = "") -> PageAction:
    """Record what the page reported back.

    First writer wins: a second POST for the same action is ignored rather than
    overwriting, because a retried answer must not resolve a request the caller
    has already been told timed out.
    """
    status = PageAction.FAILED if error else PageAction.DONE
    updated = PageAction.objects.filter(pk=action.pk, status=PageAction.PENDING).update(
        status=status, result=result, error=error or "", resolved_at=timezone.now()
    )
    action.refresh_from_db()
    if not updated:
        # Already expired or answered. Not an error for the page to discover
        # late — it just has nothing left to settle.
        pass
    return action
