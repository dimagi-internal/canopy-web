"""The Claude Code transcript core, shared by the server and both runners.

Re-exports the names consumers actually use so a caller writes
`from canopy_transcript import conversational_messages` rather than reaching
into a submodule.
"""
from .batching import TRANSCRIPT_BATCH_MAX_BYTES, chunk_raw_lines
from .paths import encode_project_dir, resolve_cli_transcript, resolve_emdash_transcript
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
    "BLOCK_STRIDE", "TOOL_INPUT_JSON_MAX", "TOOL_INPUT_STR_MAX", "TOOL_TEXT_MAX",
    "TRANSCRIPT_BATCH_MAX_BYTES", "TailReader", "assistant_text", "chunk_raw_lines",
    "compose_index", "conversational_messages", "encode_project_dir", "end_index",
    "read_records", "resolve_cli_transcript", "resolve_emdash_transcript",
    "row_payload", "rows_for_record", "scrub", "user_text",
]
