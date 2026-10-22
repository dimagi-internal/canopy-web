"""Push hears about chat turns and dialogs through the harness's signals; the
harness's turn and report paths never import apps.push (apps/push/signals.py)."""

import ast

from tests._source import iter_app_sources

NO_PUSH = {"apps/harness/services.py", "apps/harness/closing.py"}


def test_harness_turn_paths_do_not_import_push():
    offenders = []
    for src in iter_app_sources(("apps/harness",)):
        if src.rel not in NO_PUSH:
            continue
        for node in ast.walk(src.tree):
            mod = node.module if isinstance(node, ast.ImportFrom) else None
            if mod and (mod == "apps.push" or mod.startswith("apps.push.")):
                offenders.append(f"{src.rel}:{node.lineno}")
    assert not offenders, offenders


def test_push_receives_the_session_signals():
    from apps.harness import signals

    for sig, uid in ((signals.session_menu_changed, "push_session_question"),
                     (signals.session_turn_enqueued, "push_cancel_finish"),
                     (signals.session_turn_finished, "push_arm_finish")):
        assert any(r[0][0] == uid for r in sig.receivers), uid
