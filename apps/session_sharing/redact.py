"""Secret scrubbing for shared transcripts. The scrubber itself lives in `apps/common/redact.py`, below
every app, because canopy's own learning reads (`apps/brain`) must use the same one and cannot import a
feature layer. This module keeps the name callers and docs already use."""
from apps.common.redact import redact_text, redact_turn  # noqa: F401
