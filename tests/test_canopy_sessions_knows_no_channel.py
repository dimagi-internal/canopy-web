"""canopy_sessions imports no channel (Slack): Slack's session routes live in
apps/slack/session_api.py, and a session row's Slack binding comes from the
reader Slack registers (apps/canopy_sessions/channel_bindings.py)."""

import ast

from tests._source import iter_app_sources


def test_canopy_sessions_never_imports_slack():
    offenders = []
    for src in iter_app_sources(("apps/canopy_sessions",)):
        for node in ast.walk(src.tree):
            mods = []
            if isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            if any(m == "apps.slack" or m.startswith("apps.slack.") for m in mods):
                offenders.append(f"{src.rel}:{node.lineno}")
    assert not offenders, offenders


def test_slack_registers_its_binding_reader():
    from apps.canopy_sessions import channel_bindings
    from apps.slack.share import binding_of

    assert channel_bindings._READERS.get("slack") is binding_of
