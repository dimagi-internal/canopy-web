"""Which emdash project owns the session a human acted on from the phone?

emdash task names are unique per PROJECT, not per emdash. Turn 22662f53
(2026-10-04) showed what driving a session by name alone costs: eva's chat
message was typed into ada's session, because both were called "editing" and
the sidecar took the first row with that label. A DELETE aimed the same way
removes another agent's session outright, and an ANSWER presses keys in it.

So every phone-triggered action is aimed by (project, task). The server sends
the project with the request (`Session.emdash_project`); this module checks it
against emdash's own DB, and — for an older server that sends none — derives it
only when the name is unambiguous. Anything less than one known project is NOT
resolved, and the caller refuses rather than guess.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import emdash

RESOLVED = ""            # project is set
ABSENT = "absent"        # no live task by that name in the project (or anywhere)
AMBIGUOUS = "ambiguous"  # several projects hold that name and none was named
UNKNOWN = "unknown"      # emdash could not be read and no project was named


@dataclass(frozen=True)
class Target:
    project: str
    reason: str = RESOLVED

    @property
    def ok(self) -> bool:
        return bool(self.project) and self.reason == RESOLVED


def resolve(emdash_db: str | None, task: str, hint: str = "") -> Target:
    """The project owning live task `task`, given the server's `hint` (may be "").

    * hint given and emdash holds the task there      -> resolved to the hint
    * hint given, emdash holds no such task there     -> ABSENT (never another
      project's same-named task)
    * hint given, emdash unreadable                   -> resolved to the hint (the
      sidecar still looks the row up under that project, so it cannot cross over)
    * no hint, exactly one project holds the task     -> resolved to it
    * no hint, none                                   -> ABSENT
    * no hint, several                                -> AMBIGUOUS
    * no hint, emdash unreadable                      -> UNKNOWN
    """
    hint = (hint or "").strip()
    projects = emdash.task_projects(emdash_db or "", task)
    if projects is None:
        return Target(hint) if hint else Target("", UNKNOWN)
    if hint:
        return Target(hint) if hint in projects else Target("", ABSENT)
    if len(projects) == 1:
        return Target(projects[0])
    return Target("", ABSENT if not projects else AMBIGUOUS)
