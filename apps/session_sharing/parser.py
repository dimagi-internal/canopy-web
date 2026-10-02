"""Parse a Claude CLI .jsonl session file into structured turn data.

Ported verbatim from ace-web's apps/ingest/parser.py — it has no Django or
ACE dependencies, so it ports as-is. Stdlib only.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ParsedTurn:
    role: str  # "user", "assistant", "tool_use", "tool_result"
    content: dict[str, Any]
    plaintext: str


@dataclass
class ParsedSession:
    cli_session_id: str
    turns: list[ParsedTurn] = field(default_factory=list)
    raw_bytes: int = 0
    line_count: int = 0


def parse_session_file(path: Path) -> ParsedSession:
    """Parse a .jsonl session file and return structured turn data."""
    raw = path.read_bytes()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    session = parse_lines(lines)
    session.raw_bytes = len(raw)
    session.line_count = len(lines)
    return session


def parse_lines(lines: Iterable[str], *, max_turns: int | None = None) -> ParsedSession:
    """Parse JSONL lines into structured turn data. Takes any iterable so a
    caller holding a stream (a turn's retained transcript, read in bounded
    chunks) never has to materialize the whole file. `max_turns` stops reading
    once that many turns are parsed — the result is then a prefix, flagged by
    `len(session.turns) >= max_turns`."""
    session = ParsedSession(cli_session_id="")

    current_assistant_text: list[str] = []
    current_msg_id: str | None = None

    for line in lines:
        if max_turns is not None and len(session.turns) >= max_turns:
            break
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping invalid JSON line: %r", line[:200])
            continue

        kind = payload.get("type")

        if kind == "system" and payload.get("subtype") == "init":
            session.cli_session_id = payload.get("session_id", "")
            continue

        if kind == "assistant":
            msg_id = payload.get("message", {}).get("id")
            blocks = payload.get("message", {}).get("content", [])

            if msg_id != current_msg_id and current_assistant_text:
                session.turns.append(ParsedTurn(
                    role="assistant",
                    content={"text": "".join(current_assistant_text)},
                    plaintext="".join(current_assistant_text),
                ))
                current_assistant_text = []
            current_msg_id = msg_id

            for block in blocks:
                block_type = block.get("type")
                if block_type == "text":
                    current_assistant_text.append(block.get("text", ""))
                elif block_type == "tool_use":
                    if current_assistant_text:
                        session.turns.append(ParsedTurn(
                            role="assistant",
                            content={"text": "".join(current_assistant_text)},
                            plaintext="".join(current_assistant_text),
                        ))
                        current_assistant_text = []
                        current_msg_id = None
                    session.turns.append(ParsedTurn(
                        role="tool_use",
                        content=block,
                        plaintext=f"Tool: {block.get('name', 'unknown')}",
                    ))
            continue

        if kind == "user":
            if current_assistant_text:
                session.turns.append(ParsedTurn(
                    role="assistant",
                    content={"text": "".join(current_assistant_text)},
                    plaintext="".join(current_assistant_text),
                ))
                current_assistant_text = []
                current_msg_id = None

            blocks = payload.get("message", {}).get("content", [])
            # A plain user prompt is a string `content`; tool_results come as
            # a list of blocks. Handle both so user prompts aren't dropped.
            if isinstance(blocks, str):
                session.turns.append(ParsedTurn(
                    role="user",
                    content={"text": blocks},
                    plaintext=blocks,
                ))
                continue
            for block in blocks:
                btype = block.get("type")
                if btype == "tool_result":
                    session.turns.append(ParsedTurn(
                        role="tool_result",
                        content=block,
                        plaintext=str(block.get("content", ""))[:500],
                    ))
                elif btype == "text":
                    session.turns.append(ParsedTurn(
                        role="user",
                        content={"text": block.get("text", "")},
                        plaintext=block.get("text", ""),
                    ))
            continue

        if kind == "result":
            if current_assistant_text:
                session.turns.append(ParsedTurn(
                    role="assistant",
                    content={"text": "".join(current_assistant_text)},
                    plaintext="".join(current_assistant_text),
                ))
                current_assistant_text = []
                current_msg_id = None
            continue

    if current_assistant_text:
        session.turns.append(ParsedTurn(
            role="assistant",
            content={"text": "".join(current_assistant_text)},
            plaintext="".join(current_assistant_text),
        ))

    return session
