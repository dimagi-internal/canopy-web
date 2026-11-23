"""The opening context of a LEAN page turn: what the page shows and what its site offers.

A turn held on a host site's page spends its first model call finding out where it
is — `current_page`, `site_tools` — before it can do anything for the person. For an
agent whose owner asked for lean page turns (`published_profiles.page_lean_of`), the
runner fetches this brief while the agent CLI starts (`GET /api/harness/turns/{id}/page-brief`)
and appends it to the session's system prompt (never the person's message), so the first
model call can go straight to `site_call`.

It is exactly what those two tools would answer, for the same turn and the same
person: the conversation's own page state (`page_state`, as `current_page` reads it
through the chat key) and the site's tools through canopy's gateway, as the person who
sent, under their own act-as-you permission (`host_gateway.resolve` / `list_tools`, which
refuse without a confirmed grant — and then the brief says so instead of listing).
Nothing here reaches further than the turn's own tools reach.
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync

#: Ids of a page's visible rows kept in the brief; the rest are counted, not listed.
MAX_VISIBLE_IDS = 40
#: The longest one-line description a non-backing tool keeps.
SHORT_DESCRIPTION = 240
#: Above this the tools' own descriptions are shortened (never their argument descriptions),
#: and past it the rest are only counted (the agent can still call `site_tools`). About 20k
#: tokens: Connect Labs' run pages list ~20 tools in about 50k characters.
MAX_BRIEF_CHARS = 80_000


def _page_state(session) -> dict:
    state = dict(getattr(session, "page_state", None) or {})
    ids = state.get("visible_ids")
    if isinstance(ids, list) and len(ids) > MAX_VISIBLE_IDS:
        state["visible_ids"] = ids[:MAX_VISIBLE_IDS]
        state["visible_ids_total"] = len(ids)
        state["visible_ids_note"] = (f"the first {MAX_VISIBLE_IDS} of {len(ids)}; "
                                     "call current_page for all of them")
    return state


def _one_line(text: str) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= SHORT_DESCRIPTION:
        return text
    cut = text[:SHORT_DESCRIPTION]
    stop = cut.rfind(". ")
    return (cut[:stop + 1] if stop > 60 else cut.rstrip()) + " …"


def _compact_schema(schema) -> dict:
    """The schema's properties and required list, with each property's own description
    kept: that is where a site states an argument's format (`worker_keys` is
    `<opportunity_id>::<username>`), and a call made without it is a guess. Only titles,
    examples and defaults are dropped."""
    if not isinstance(schema, dict):
        return {}

    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in ("title", "examples", "default")}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    out = {"properties": strip(schema.get("properties") or {})}
    if schema.get("required"):
        out["required"] = list(schema["required"])
    return out


def _tool_line(t: dict, *, short: bool) -> str:
    desc = " ".join(str(t.get("description") or "").split())
    desc = _one_line(desc) if short else desc
    schema = json.dumps(_compact_schema(t.get("input_schema")), separators=(",", ":"))
    return f"- {t.get('name')}: {desc} input: {schema}"


def tools_text(tools: list[dict], *, backing: str = "") -> list[str]:
    """One entry per tool: its name, its whole description and its input schema with the
    property descriptions. Only when the whole list would pass `MAX_BRIEF_CHARS` do the
    tools after the page's own backing tool get one-line descriptions, and past it the
    rest are only counted (the agent can still call `site_tools`)."""
    named = [t for t in tools if str(t.get("name") or "")]
    # The page's own backing tool first: it is the one the page is about.
    named.sort(key=lambda t: 0 if t.get("name") == backing else 1)
    full = [_tool_line(t, short=False) for t in named]
    if sum(len(line) for line in full) <= MAX_BRIEF_CHARS:
        return full
    lines, size = [], 0
    for i, t in enumerate(named):
        line = full[i] if t.get("name") == backing else _tool_line(t, short=True)
        size += len(line)
        if size > MAX_BRIEF_CHARS:
            lines.append(f"- … {len(named) - len(lines)} more: call site_tools for the full list")
            break
        lines.append(line)
    return lines


#: How to call a site's tools from its page, for every site: the page's own scope and ids,
#: as given. A tool asked for a wider or a different scope than the page shows refuses
#: (the site keeps an agent to what the person is looking at), and an id rebuilt from a
#: fragment of one is a guess.
SCOPE_RULE = (
    "Call the site's tools with exactly the scope the page's `filters` give (for example its "
    "run, and its program OR its opportunity, whichever the page names), not a wider or a "
    "narrower one of your own. When the question is about \"this\" or \"these\", use the ids "
    "the page gives, as given: `filters.drilled` and `visible_ids` are already in the format the "
    "tools take, so pass them unchanged rather than taking them apart. If a tool refuses, read "
    "its error, fix the call it points to, and say what you could not read rather than guessing "
    "at an answer.")


def build(turn) -> dict:
    """The brief for `turn`: `{"text", "site", "tools", "refused"}`. `refused` is the
    gateway's refusal ("" when the tools were listed), said to the agent in the text."""
    from apps.agents import published_profiles  # noqa: PLC0415 — the interface reads models, loaded with a call
    from apps.host_grants import host_gateway  # noqa: PLC0415 — the gateway loads with a call

    session = turn.chat_session if turn.chat_session_id else None
    site = published_profiles.page_site_of(turn)
    if session is None or not site:
        return {"text": "", "site": "", "tools": 0, "refused": "not_on_a_page"}
    state = _page_state(session)
    refused = ""
    tools: list[dict] = []
    scope = ""
    try:
        ctx = host_gateway.resolve(str(turn.pk), reason="see what it can do for you on this page")
        tools = async_to_sync(host_gateway.list_tools)(ctx)
        scope = ctx.scope
    except host_gateway.GatewayRefusal as exc:
        refused = exc.message
    from apps.agents import published_profiles as pp  # noqa: PLC0415 — as above

    guidance = pp.page_guidance_of(turn.acting_agent, site)
    parts = [
        f"<page_context site=\"{site}\">",
        f"You are answering in the agent panel on a {site} page, for the person who sends the "
        "next message. This is what you would get from current_page and site_tools, fetched "
        "for you as this conversation started: use it, and call site_call straight away. Call "
        "current_page or site_tools again only if you need something this leaves out "
        "(a full tool description, or the page after it changes).",
        "",
        "The page (current_page):",
        json.dumps(state, separators=(",", ":"), default=str),
        "",
        SCOPE_RULE,
    ]
    if guidance:
        # The agent's own words for this site's pages (`page_guidance:` on its entry), in
        # place of the CLAUDE.md and skills a lean turn does not load.
        parts += ["", f"<agent_guidance for=\"{site}\">", guidance, "</agent_guidance>"]
    if refused:
        parts += ["", f"{site}'s tools could not be listed for them yet: {refused}"]
    else:
        parts += ["", f"{site} tools you may call with site_call, as the person"
                  + (f" (scope: {scope})" if scope else "") + ":"]
        parts += tools_text(tools, backing=str(state.get("backing_tool") or ""))
    parts.append("</page_context>")
    return {"text": "\n".join(parts), "site": site, "tools": len(tools), "refused": refused}
