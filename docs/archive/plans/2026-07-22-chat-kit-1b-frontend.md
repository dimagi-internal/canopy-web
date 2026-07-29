# Chat Kit 1b — Frontend: canopy-ui/chat kit + standalone route — Implementation Plan

> **For agentic workers:** port ace-web's chat frontend into a reusable `canopy-ui/chat` kit and wire a standalone canopy-web route that proves it against the plan-1a backend. TDD the pure modules; verify components via `npm run build` (tsc + vite) + `npm test` (vitest).

**Goal:** A reusable, app-agnostic chat kit in `canopy-ui/chat` (protocol types, reducer, hooks, presentational `ChatPanel` tree) plus a canopy-web `/w/:workspace/chat/:id` route that connects to `ws/chat/{id}/` and holds a live, multiplayer-capable conversation.

**Architecture:** Port ace-web's kit (`/Users/jjackson/emdash-projects/ace-web/frontend/src`) with adaptations. Pure modules + reducer copy ~verbatim. The kit is presentational/props-in-callbacks-out; app specifics (ws URL, markdown renderer, session-meta) are injected. `current_user_id` comes from the canonical `session.state` snapshot (canopy `MeOut` has no numeric id).

**Tech stack:** React 19, Vite, Tailwind v4, canopy-ui primitives (`canopy-ui/ui`), vitest. Canonical protocol = plan 1a (ace-web contract).

## Global Constraints

- Kit lives in `frontend/packages/canopy-ui/src/chat/`, exported via a new `./chat` subpath in `packages/canopy-ui/package.json` `exports`.
- The kit imports ONLY: react, `canopy-ui/ui` primitives, `canopy-ui/lib` (`cn`), `lucide-react`. **No** `react-markdown` (canopy-ui stays dep-light) — markdown is an injected `renderMarkdown?: (text:string)=>ReactNode` prop, defaulting to a plain-text renderer. **No** app routing/auth/api imports.
- Canonical wire types (plan 1a): actions `chat.send`/`chat.stop{message_id}`/`draft.update{version,body}`/`draft.take_over`/`draft.discard`/`presence.heartbeat`; events `session.state`, `session.error`, `chat.stream_start`/`chat.delta`/`chat.tool_use`/`chat.tool_result`/`chat.stream_complete`/`chat.stream_error`/`chat.stream_cancelled`, `draft.updated`/`draft.committed`/`draft.discarded`/`draft.lock_changed`, `presence.joined`/`presence.left`. **`message_id` is a `string`** (canopy sends string ids).
- **Adaptation vs ace:** `chat.stream_start` must **create the assistant message if absent** (canopy can't pre-send its id in `draft.committed`). Keep ace's `draft.committed`→optimistic user+placeholder logic for the USER message; for the assistant, upsert on `stream_start`.
- Strip all ace-specific infra: `useCliAuthStatus`/CLI banners, opp/step coupling, ace `WelcomePanel` suggestions (→ `emptyState` slot), `X-ACE-Workspace`/`csrftoken_ace` client specifics.
- Verify after each group: `cd frontend && npm test` (pure modules) and `npm run build` (whole app typecheck+bundle) stay green.

---

### Task 1: Protocol types (`canopy-ui/src/chat/protocol.ts`)

Port ace `src/api/types.ws.ts:16-132` (enums, `Message`, `Draft`, `Participant`, `SessionState`, `WsAction`, `WsEvent`). Change `Message.id` and all `message_id` fields to `string`. Drop opp fields. No test (types only) — it compiles under `tsc`.

### Task 2: `sessionReducer` (`canopy-ui/src/chat/sessionReducer.ts` + `.test.ts`)

Port ace `src/hooks/sessionReducer.ts` verbatim, with two edits: (a) `message_id` comparisons treat ids as strings; (b) `chat.stream_start` — if no message with that id exists, **append** a new `{id, role:"assistant", status:"streaming", plaintext:"", turn_index}` before setting streaming (upsert). Drop the `notifySessionsUpdated` import (make it an optional injected callback param on the reducer's owning hook, not the reducer). Port ace's reducer unit tests (`sessionReducer.test.ts`) adapting to string ids + the upsert behavior; add one test: `chat.stream_start` for an unknown id creates the assistant message.

### Task 3: `drafts` + `pairToolMessages` (`canopy-ui/src/chat/` + tests)

Copy ace `src/lib/drafts.ts` (`isDraftIdle`, `msUntilDraftIdle`, `IDLE_THRESHOLD_MS`) and `src/components/chat/pairToolMessages.ts` (`pairToolMessages`, `deriveToolStatus`, `toolPreview`, `toolDisplayName`) ~verbatim. Port their tests if present; else add: draft idle math (before/after 2s), and tool pairing (use→result matched, unmatched result standalone, streaming pair with null result).

### Task 4: `useStickyBottom` + `useSessionSocket` (`canopy-ui/src/chat/`)

- `useStickyBottom(dep)` — copy ace `src/hooks/useStickyBottom.ts` verbatim.
- `useSessionSocket({ sessionId, wsUrl })` — port ace `src/hooks/useSessionSocket.ts`. Inject `wsUrl: (path)=>string` (no ace `wsUrlFor`); path = `ws/chat/${sessionId}/`. Keep: reconnect backoff `[1000,2000,5000,10000]`, 20s `presence.heartbeat`, optimistic debounced (150ms) `draft.update`, pending `chat.stop` queue. Rename outbound send verb to canonical `chat.send`, draft field to `version` (already canonical), keep `draft.take_over`/`draft.discard`/`chat.stop`. Returns `{state, connected, lastError, sendChat, stopChat, updateDraft, takeOverDraft, discardDraft}`. `applyEvent` intercepts `session.error` (clear pending draft on `draft_version_mismatch`) then reduces. Unit-test `mergeEvents`-equivalents if factored; otherwise this hook is covered by the build + the route smoke.

### Task 5: Presentational components (`canopy-ui/src/chat/`)

Port with canopy-ui primitives + tokens + injected `renderMarkdown`:
- `ToolCallPair.tsx`, `MessageItem.tsx`, `MessageList.tsx` (from ace equivalents) — bubbles, tool disclosure, streaming "Thinking…"/cursor, neutral error styling. `MessageList` takes an `emptyState?: ReactNode` (replaces ace `WelcomePanel`).
- `SendBox.tsx` — soft-lock gating (`canEdit`), take-over button, Enter-to-send, stop button, idle-tick timer. **Remove** all CLI-auth props/banners; add optional `banner?: ReactNode` + `disabledReason?: string` slots.
- `PresenceChips.tsx`, `ConnectionStatus.tsx` — port ~verbatim.
- `ChatPanel.tsx` — props-in/callbacks-out: `{ state, connected, currentUserId, onSend, onStop, onUpdateDraft, onTakeOver, onDiscard, renderMarkdown?, banner?, emptyState? }`. Composes header (`ConnectionStatus`+`PresenceChips`) → `MessageList` in a `useStickyBottom` container → `SendBox`. Owns the idle-tick + sticky dep internally. NO data fetching / WS / CLI-auth.
- `index.ts` — barrel exporting `ChatPanel`, `useSessionSocket`, `sessionReducer`, types, `drafts`, `pairToolMessages`.

### Task 6: canopy-ui `./chat` export

Add `"./chat": "./src/chat/index.ts"` to `packages/canopy-ui/package.json` `exports`. Verify `import { ChatPanel } from 'canopy-ui/chat'` typechecks from canopy-web.

### Task 7: App container — `src/api/chat.ts` + `ChatPage` + route

- `frontend/src/api/chat.ts` — REST wrappers (plain fetch like `src/api/sessions.ts`, CSRF on mutations): `createSession({title?, agentSlug?})` → `POST /api/chat/`, `getSession(id)` → `GET /api/chat/{id}`, `listSessions()` → `GET /api/chat/`. Types from `src/api/generated.ts` where present.
- `frontend/src/pages/ChatPage.tsx` — route `/w/:workspace/chat/:id`. Calls `useSessionSocket({ sessionId: id, wsUrl })` (canopy `wsUrl` from `src/lib/wsUrl`), reads `currentUserId = state.current_user_id`, passes a react-markdown `renderMarkdown` (react-markdown + remark-gfm, already a canopy-web dep) into `<ChatPanel/>`. Minimal shell (title + the panel). A `?new=1`/missing-session flow may `createSession` then redirect.
- `frontend/src/router.tsx` — add `{ path: '/w/:workspace/chat/:id', element: <ChatPage /> }` (lazy) under the `AppLayout` children, inside the guarded tenant block.

### Task 8: Verify end-to-end

- `cd frontend && npm test` — pure-module unit tests green.
- `cd frontend && npm run build` — tsc + vite bundle clean (no type errors; the kit + route compile).
- Manual/smoke (optional, slice-2 will formalize): render the route against a running backend; confirm connect → snapshot → type draft → send → streamed stub reply. Per project memory, verify the real rendered page (PWA/service-worker), not curl.

## Self-Review

- Coverage: protocol (T1), reducer+upsert (T2), drafts/tools (T3), hooks+keepalive+optimistic draft (T4), the full presentational tree with injected seams (T5), the reusable export (T6), the app container+route (T7), verification (T8). ace-specific infra explicitly stripped (constraints). ace-web adoption: the kit is import-clean and seam-injected, so ace-web later swaps its local components for `canopy-ui/chat` (spec Stage A).
- Placeholders: none — each task names exact ace source files + exact canopy targets + adaptations.
- Types: `message_id: string` consistent across protocol/reducer/hook/components; `ChatPanel` prop names match `useSessionSocket` return names (`onSend`↔`sendChat`, etc. — the container maps them).
