"""Page-key parsing and Channels group naming for presence.

Pure functions — no Redis, no async — so they unit-test without any
infrastructure. Page keys arrive from the client and are therefore parsed
defensively: anything that is not exactly `<app>:<workspace>:<resource>`
is rejected rather than coerced.

The workspace segment is the AUTH segment — the consumer's membership gate
reads it and nothing else — so it is charset-validated here rather than
passed through. Two consequences worth stating explicitly:

* Globally-scoped pages use the `~global` sentinel, not the bare word
  `global`. A leading `~` cannot match `WORKSPACE_RE`, so no real workspace
  slug can ever collide with the sentinel by charset. (Workspace creation
  does not itself enforce a charset, so the consumer ALSO checks that no
  row actually shadows the sentinel — see presence_consumer.)
* A workspace whose slug contains a colon can never be addressed: the
  bounded split can only ever put the text BEFORE the first separator in
  the workspace slot, and that text is what the membership gate checks.
  Such a workspace gets no presence rather than borrowing another
  tenant's gate.
"""
from __future__ import annotations

import hashlib
import re

#: Workspace segment for pages that are not tenant-scoped. The leading `~`
#: is deliberate: it cannot match WORKSPACE_RE, so the sentinel is not a
#: value any client can also assert as a real workspace slug.
GLOBAL_SENTINEL = "~global"

#: Workspace slugs are `CharField(primary_key=True, max_length=64)`; this is
#: the shape presence is willing to treat as a tenant. Anything else (a
#: colon, an uppercase letter, whitespace, a leading `~`) is rejected.
WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: The app segment namespaces rosters across sibling deployments.
APP_RE = re.compile(r"^[a-z0-9-]{1,32}$")

#: Bound on the whole key. Resources are free-form, so without a cap a client
#: could push arbitrarily large strings through the parser into Redis keys.
MAX_PAGE_KEY_LEN = 512


def parse_page_key(page_key: str) -> tuple[str, str, str] | None:
    """Split `<app>:<workspace>:<resource>` into its three parts.

    The resource may itself contain colons (`opp:bednet/run-001`), so the
    split is bounded to 2. Returns None for anything malformed — callers
    MUST treat None as "reject this frame", never as "use a default".
    """
    if not page_key or len(page_key) > MAX_PAGE_KEY_LEN:
        return None
    parts = page_key.split(":", 2)
    if len(parts) != 3:
        return None
    app, workspace, resource = (p.strip() for p in parts)
    if not APP_RE.match(app):
        return None
    if workspace != GLOBAL_SENTINEL and not WORKSPACE_RE.match(workspace):
        return None
    if not resource:
        return None
    return app, workspace, resource


def group_name(page_key: str) -> str:
    """A Channels-legal group name for a page key.

    Channels group names permit only ASCII alphanumerics, hyphens, periods
    and underscores (max 100 chars). Page keys contain ':' and '/', so the
    key is hashed rather than sanitised — sanitising would let two distinct
    keys collide onto one group.
    """
    digest = hashlib.sha1(page_key.encode("utf-8")).hexdigest()[:32]
    return f"presence.{digest}"
