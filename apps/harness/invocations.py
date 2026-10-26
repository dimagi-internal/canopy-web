"""A turn's leading plugin command, read in one place.

A prompt whose first line is `/<plugin>:<skill> <args>` asks the agent CLI to run
that skill of that plugin — `/echo:turn`, `/ace:ask --thread 7`. canopy-web keeps
and shows the prompt exactly as written (the canonical text, which is also how a
person types one into a chat), and hands the claiming runner the parsed
`Invocation` beside it, so each turn executor renders it in its own CLI's terms —
Claude Code types the line as a slash command; a CLI that loads skills but not
commands invokes the skill of the same name — rather than every executor parsing
Claude's syntax again. Whatever follows the first line (a dispatch's provenance
note, a brief) is `rest`, carried as it is.

Everything that writes a command builds it with `command`, so the syntax lives here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A plugin and a skill name as Claude Code and the plugin marketplace spell them.
_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_FIRST_LINE = re.compile(rf"/({_NAME}):({_NAME})(?:[ \t]+(.*))?")


@dataclass(frozen=True)
class Invocation:
    plugin: str
    skill: str
    #: The rest of the command's own line, as typed ("--thread 7").
    args: str = ""
    #: Every line after the command's, as written.
    rest: str = ""

    @property
    def name(self) -> str:
        """`plugin:skill`, the name both CLIs list the skill under."""
        return f"{self.plugin}:{self.skill}"


def parse(prompt: str | None) -> Invocation | None:
    """The plugin command `prompt` opens with, or None for plain words."""
    text = (prompt or "").lstrip()
    first, _, rest = text.partition("\n")
    m = _FIRST_LINE.fullmatch(first.rstrip())
    if m is None:
        return None
    return Invocation(m.group(1), m.group(2), (m.group(3) or "").strip(), rest.strip("\n"))


def command(plugin: str, skill: str, args: str = "") -> str:
    """The canonical text of a plugin command: `/plugin:skill args`."""
    line = f"/{plugin}:{skill}"
    if args.strip():
        line = f"{line} {args.strip()}"
    if parse(line) is None:
        raise ValueError(f"not a plugin command: {line!r}")
    return line
