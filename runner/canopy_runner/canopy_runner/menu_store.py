"""Pending blocked-agent dialogs, across a runner restart.

**Why this has to exist.** The hook that raises a dialog fires exactly once. Hold
the menu only in memory and a restart does not merely forget it — the next
session report ships `question: null`, which *retires* the menu server-side, and
nothing can ever rediscover it: no hook re-fires, and the transcript will not
carry the ask until it is answered. The runner auto-updates itself every 30
minutes, so a restart is routine. That combination turns "the agent is waiting on
you" into silence, which is the exact failure this whole path exists to end.

**Why a plain JSON file.** The data is small, per-box, and worthless off it — the
menu describes a dialog drawn on THIS machine's terminal. A restored menu may be
stale (answered at the laptop while the runner was down), and that is handled
where it belongs rather than here: a tap re-reads the real screen, finds no
dialog, and returns `no_dialog`, which both tells the human and clears the menu.
Verifying at startup instead would mean a CDP read per restored menu, which
CLICKS the task and steals focus — the thing #510 was reverted for.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("canopy_runner.hooks")

# A key is (project, emdash task); JSON has no tuple keys, so rows are stored as
# a list of records rather than an object. Explicit beats a "project\ttask"
# string that some future reader has to know to split.
_PROJECT, _TASK, _MENU = "project", "task", "menu"

# `marker_from_hook`'s source tag. Kept as a literal rather than imported so this
# module stays free of canopy_transcript, which is what lets it unit-test alone.
_NOTIFICATION = "notification"


class MenuStore:
    """Load/save `{(project, task): menu}` at `path`. Never raises."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[tuple[str, str], dict]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except Exception:  # noqa: BLE001 — a corrupt file is an empty one
            logger.debug("pending-menu store unreadable at %s", self.path, exc_info=True)
            return {}
        out: dict[tuple[str, str], dict] = {}
        for row in raw if isinstance(raw, list) else ():
            if not isinstance(row, dict) or not isinstance(row.get(_MENU), dict):
                continue
            project, task = row.get(_PROJECT), row.get(_TASK)
            if not isinstance(project, str) or not isinstance(task, str) or not task:
                continue
            # A notification marker is a claim that a turn was in flight, and
            # turn state is deliberately NOT persisted — after a restart we do
            # not know, and the safe answer there is silence. Restoring one
            # therefore restores a claim nothing can still justify, and it
            # cannot decay on its own: the `Stop` that would retire it belongs
            # to a turn that ended while this process was not running.
            #
            # Observed: `spark` carried "Claude is waiting for your input" across
            # a restart and sat on the fleet's waiting list with no way to clear
            # it. A real menu is different and IS restored — it describes a
            # dialog still drawn on a screen, and a tap verifies it there.
            if row[_MENU].get("source") == _NOTIFICATION:
                continue
            # Marked so an operator reading a report can tell a menu observed this
            # process from one carried across a restart. Nothing branches on it —
            # a restored menu is the best information available, and a tap
            # verifies it against the real screen anyway.
            out[(project, task)] = {**row[_MENU], "restored": True}
        return out

    def save(self, menus: dict[tuple[str, str], dict]) -> None:
        rows = [{_PROJECT: p, _TASK: t, _MENU: m} for (p, t), m in menus.items()]
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(rows))
            # Atomic replace: a torn write here would be read back as a corrupt
            # file on the next start, which loses every pending menu at once.
            tmp.replace(self.path)
        except Exception:  # noqa: BLE001
            logger.debug("could not write the pending-menu store at %s", self.path,
                         exc_info=True)
