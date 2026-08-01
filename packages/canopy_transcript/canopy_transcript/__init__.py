"""The Claude Code transcript core, shared by the server and both runners.

Re-exports the names consumers actually use so a caller writes
`from canopy_transcript import conversational_messages` rather than reaching
into a submodule.
"""
from .batching import TRANSCRIPT_BATCH_MAX_BYTES, chunk_raw_lines
from .hooks import (
    ACTIVITY_EVENTS,
    FORWARDED_EVENTS,
    STATUS_COMPLETE,
    STATUS_PENDING,
    activity_for_hook,
    events_for_hook,
    rows_for_hook,
)
from .paths import (
    emdash_task_candidates,
    encode_project_dir,
    parse_emdash_worktree,
    resolve_cli_transcript,
    resolve_emdash_transcript,
)
from .noise import SYSTEM_NOISE_PREFIXES, is_system_noise
from .questions import ASK_TOOL, hook_retires_menu, menu_from_hook, pending_question
from .records import read_records
from .rows import (
    BLOCK_STRIDE,
    TOOL_INPUT_JSON_MAX,
    TOOL_INPUT_STR_MAX,
    TOOL_TEXT_MAX,
    assistant_text,
    compose_index,
    conversational_messages,
    end_index,
    row_payload,
    rows_for_record,
    scrub,
    user_text,
)
from .tail import TailReader

__all__ = [
    "ACTIVITY_EVENTS", "ASK_TOOL", "BLOCK_STRIDE", "FORWARDED_EVENTS",
    "STATUS_COMPLETE", "STATUS_PENDING", "pending_question", "hook_retires_menu",
    "menu_from_hook",
    "SYSTEM_NOISE_PREFIXES", "is_system_noise",
    "TOOL_INPUT_JSON_MAX", "TOOL_INPUT_STR_MAX", "TOOL_TEXT_MAX",
    "TRANSCRIPT_BATCH_MAX_BYTES", "TailReader", "assistant_text", "chunk_raw_lines",
    "compose_index", "conversational_messages", "encode_project_dir", "end_index",
    "activity_for_hook", "emdash_task_candidates", "events_for_hook", "parse_emdash_worktree",
    "read_records", "resolve_cli_transcript",
    "resolve_emdash_transcript", "rows_for_hook",
    "row_payload", "rows_for_record", "scrub", "user_text",
]
