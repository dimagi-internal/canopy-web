"""Every chat door sends through `apps/canopy_sessions/doors.py::send_from`.

The preamble a door owes a send (the site's runner requirements, the capped
client nonce, a refusal as a 422, the dev stub inline) lives there once; a door
calling `services.send_message` directly would quietly skip part of it.
"""

import ast

from tests._source import iter_app_sources

ALLOWED = {
    "apps/canopy_sessions/services.py",  # defines it
    "apps/canopy_sessions/doors.py",     # the one caller
}


def _calls(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) == name:
                yield node


def test_only_doors_calls_send_message():
    offenders = [f"{src.rel}:{call.lineno}"
                 for src in iter_app_sources(("apps", "config"), exclude=ALLOWED)
                 for call in _calls(src.tree, "send_message")]
    assert not offenders, (
        "send through apps.canopy_sessions.doors.send_from, not services.send_message: "
        + ", ".join(offenders))


def test_every_door_is_declared():
    from apps.canopy_sessions import doors

    used = {call.args[0].value
            for src in iter_app_sources(("apps", "config"))
            for call in _calls(src.tree, "send_from")
            if call.args and isinstance(call.args[0], ast.Constant)}
    assert used, "no door sends through send_from"
    assert used <= set(doors.DOORS), used - set(doors.DOORS)
