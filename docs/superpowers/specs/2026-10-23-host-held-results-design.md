# Host-held results — the host owns what a tool produced; the agent and the person's views read it from the host, as themselves

**Status:** design, agreed with Jonathan 2026-10-23. Builds on the MCP Apps host
(`2026-10-08-mcp-apps-host-design.md`), the host grant contract, and sensitive
conversations (`2026-10-22-sensitive-data-agents-design.md`,
`2026-10-23-sensitive-conversations-sealed-to-their-people-design.md`).

## The problem

A View in the chat (an MCP Apps card, e.g. Labs' query results) renders from the tool
result canopy kept. That makes canopy's conversation record the data source for the UI,
which breaks the moment canopy must not keep the data: in a sensitive conversation the
result is stripped, so Labs' card waits forever, or re-runs the agent's query as the
viewer (twice the work, and possibly different rows from the ones the agent reasoned on).
It also caps the chat at "what fits in a tool result": no paging over a large result, no
re-sorting, no live update when the data changes, no shared artifact.

## The model

The **host** owns what its tools produce. canopy carries the conversation and the wiring,
never the data. One design for every conversation; sensitivity only changes what canopy
**stores**.

1. **A tool returns facts and resources.** Facts for the model (a summary: row count,
   columns, a few sample values, what the query was) plus one or more MCP
   **`resource_link`** content items (MCP 2025-06-18 tool-result content) naming
   artifacts the host keeps: a result set, a chart spec, a draft edit and its diff, an
   indicator breakdown. Each link carries `uri`, `name`, `mimeType` and optional
   `description`; the URI is the host's, e.g. `labs-result://<id>`.
2. **The host keeps the artifact, for the person.** Stored host-side with an owner (the
   visitor the call ran as), the tool call it came from, and an expiry (default 24 h,
   host's choice). Reading it is an MCP `resources/read` checked against the reader: the
   owner may; anyone else the host's own ACL would let see the underlying data may (a
   colleague on the same conversation); nobody else. The host may recompute on read
   (fresh) or serve the stored snapshot (what the agent saw); the resource says which.
   Large results page through the resource (a `?page=`/cursor parameter in the URI or
   `_meta`), never through the chat.
3. **Views read and subscribe as the viewer.** A View is handed the tool result's
   resource links (in `ui/notifications/tool-result`, even when canopy stripped
   everything else) and calls `resources/read` through the host bridge → canopy's View
   routes → `host_gateway` (DPoP, the viewer's delegated token). `resources/subscribe`
   is honoured: the host's `notifications/resources/updated` reaches the View, which
   re-reads. The host's own ACL and audit apply to every read.
4. **People act in views, the agent through tools, on the same objects.** A View calls
   the host's tools as the person (filter, drill in, confirm a previewed edit); tells
   the agent what the person did (`ui/update-model-context`, `ui/message`); the agent can
   open a new View on an artifact by returning its link.
5. **The agent reads artifacts too.** The model gets the facts; when it needs rows it
   reads the resource through the gateway (`site_read` alongside `site_call`), as the
   visitor, with the same ACL — so a large result never has to be in context.
6. **What canopy stores.** For every conversation: the tool call's metadata and the
   result's resource links (URI, name, mimeType — addresses, not data). For an ordinary
   conversation canopy may also keep the facts and any inline content, as today. For a
   sensitive conversation it keeps only the links: `sensitivity.strip_result` keeps
   `resource_link` items (validated shape, URI scheme on the host's declared list) and
   drops everything else.

## The SDK's host half: artifacts in a line

`canopy_sdk` gains a host helper so a host returns "facts + an artifact" without building
storage, expiry or ACL itself:

- `canopy_sdk.host.artifacts`: `save(owner, kind, payload, *, tool_call_id, ttl, scheme)`
  → a `resource_link` dict; `read(uri, reader)` → the payload or a named refusal; an
  owner check plus an optional host ACL hook (`may_read(reader, artifact)`), and expiry.
- `canopy_sdk.django`: a model + migration for the store (payload JSON, owner subject,
  kind, tool call id, created/expires), a sweep, and a ready-made MCP resource handler for
  `resources/read` (and a template listing) the host mounts on its MCP server.
- Facts helper: `summarize_rows(columns, rows, sample=5)` for the common table case.

## Labs

- `data_query` (and the workflow tools that return tables: run indicators, workflow
  context's pipeline previews) return facts + a `labs-result://` link via the SDK helper.
- The results View (`connect_labs/mcp/ui/data_result.html`) reads the link with
  `resources/read`, pages and sorts through it, and drops "ask again".
- `resources/read` for `labs-result://` runs as the visitor; a reader who could not run
  the query cannot read its result.

## canopy-web

- Strip keeps `resource_link`s for sensitive conversations; the View gets them in its
  tool-result notification; the stored row's metadata lists them.
- `resources/subscribe` through the View routes; host updates fan out to the View.
- The agent's `site_read` tool (resources/read through the gateway), declared alongside
  `site_call` and allowed in a `data: sensitive` capability.
- Docs: `docs/architecture/embedding-a-canopy-agent.md` (host side: return artifacts),
  `docs/architecture/api-surface.md`.

## Rollout

1. SDK host artifacts + canopy (strip keeps links, View gets links, subscribe,
   `site_read`) — dimagi-canopy 0.18.0.
2. Labs: tools return facts + links; the results View reads them.
3. The red team's canary check extends to artifacts: nothing canopy stores for a
   sensitive conversation holds a row.
