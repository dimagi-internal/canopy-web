# AI for app pages (`canopy.ai.complete`)

A workspace app's page (JSX or bundle) runs in a frame with no network, so it cannot call a model
itself, and it must not carry a person's pasted API key. Instead it asks canopy:

```js
const answer = await canopy.ai.complete({
  model: 'fast',                       // or 'smart': an alias, never a model id
  system: 'You summarize sales history.',
  messages: [{ role: 'user', content: 'Summarize: …' }],
  max_tokens: 800,                     // optional, 1–8192
  json_schema: { type: 'object', … },  // optional: answer.json is the parsed answer
  tools: [{ name, description, input_schema }],  // optional, not with json_schema
  web_search: false,                   // optional (Gemini grounding)
  feature: 'record-summary',           // optional label kept in canopy's log
})
// → { text, content, stop_reason, model, usage, json? }
```

canopy-web calls the model with **its own** credential and answers the page. Code:
`apps/workspace_apps/ai.py` (the rules are its module docstring), the route
`POST /api/workspace-apps/{workspace}/{key}/ai/complete` (`api.py::ai_complete_workspace_app`), the
frame side in `frontend/src/dynamicViews/protocol.ts` (`AiRequest`, an `app` op `ai`) and
`apps/workspace_apps/bundle_client.js` (`canopy.ai`).

## Rules

- **Declared in the manifest.** `"ai": {"purpose": "<what the pages use it for>"}` (`manifest.py::_validate_ai`).
  Without it every call is a 403. The store shows the purpose (`services.store_detail`), and a version that
  ADDS it is a reach change an admin publishes (`template_diff.changes`); dropping it widens nothing.
  Template-only: no overlay op reaches it.
- **Member pages only, as the viewer.** `frontend/src/api/workspaceApps.ts::callApp` refuses it on a public
  or link page; the route is a member's (`_installation` without `guest_ok`). A draft's test copy may call it
  (a maintainer trying the page); the log records the space.
- **Aliases, in one place.** `APP_AI_MODELS` (`config/settings/base.py`, env `APP_AI_FAST_MODEL` /
  `APP_AI_SMART_MODEL`) maps `fast` and `smart` to a model; a page never names one. `vertex/<gemini>` is Gemini
  on Vertex (`generateContent`); `vertex/anthropic/<claude>` is Claude on Vertex (`rawPredict`). Switching
  provider is a settings change. `APP_AI_LOCATION` is the Vertex location (`global`: where Gemini 3.x and
  Claude are served).
- **Provider-neutral shape.** A message's content is text or blocks — `text`, `tool_use` (assistant) and
  `tool_result` (user) — translated per provider (`ai._gemini_body` / `ai._gemini_answer`,
  `ai._claude_body` / `ai._claude_answer`). A block's `signature` is opaque (Gemini's thought signature): the
  page carries it back on its next turn and canopy never reads it. On Claude a `json_schema` becomes one forced
  tool whose input is the answer. Gemini's thinking parts are dropped; its thinking counts against its own
  output budget, so canopy asks for `max_tokens + THINKING_HEADROOM`.
- **canopy's identity, memory's zero-retention gate.** The call goes out on memory's Vertex session
  (`apps/contacts/embeddings.py::_vertex_session`: workload identity from the ECS task role, no stored Google
  key) and only once the project is verified to keep nothing (`embeddings.check_retention`, cached for
  `RETENTION_TTL` per model). Off — a 503 the page shows — when `HCP_VERTEX_PROJECT` is empty or the check fails.
- **Attributed, logged, limited.** Every call that passes the declaration check is an `AppAiCall` row: the
  viewer, the app, the space, the alias, the model it named then, the page's `feature` label, tokens, time and
  outcome (`ok`, `failed`, `refused`). Never the prompt or the answer. The same rows are the per-viewer limit
  across every app (`APP_AI_PER_HOUR`, `APP_AI_PER_DAY`; a 429 problem).

## Errors a page sees

The promise rejects with a sentence: 403 (the app does not declare `ai`), 422 (a malformed request, or a
model id instead of an alias), 413 (over `MAX_REQUEST_CHARS`), 429 (the viewer's limit), 502 (the model
failed), 503 (AI off, provider unverified, or the model busy).

## Gotchas

- **Claude needs enabling in the GCP project's Model Garden** before a `vertex/anthropic/…` alias answers: until
  then Vertex says 404 "Publisher model … not found or your project does not have access". Gemini needs
  nothing beyond the service account memory already uses.
- **A model id goes stale.** Vertex retires models; when an alias starts failing with that same 404, point it at a
  current model (env var on the task definition, `deploy/aws/canopy-web.cfn.yaml`, or the default in
  `config/settings/base.py`). Pages never change.
