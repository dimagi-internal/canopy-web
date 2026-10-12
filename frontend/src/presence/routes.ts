import type { RouteRule } from 'canopy-ui/presence'

/**
 * canopy-web's route table for presence grouping.
 *
 * A route with no rule here yields `pageKeyFor(...) === null`, which opens no
 * socket and renders no badge (see `usePresence` and `PresenceBadge`). That is
 * the mechanism this list is built on: **presence is opt-in, page by page.**
 *
 * The bar is COLLISION, not company. A surface earns presence when two people
 * can act on the same object at the same time and silently clobber each other
 * — a co-edited draft, a scene's text, a narrative under review. It does not
 * earn presence merely because two people can be reading it: the badge used to
 * cover nearly every route, so "who else is on Timeline" was noise sitting in
 * the header of pages where nobody could collide with anyone.
 *
 * Deliberately NOT here, though each once had a rule:
 *   - the workspace index, members, timeline, walkthroughs, inbound, and the
 *     agents / chat / DDD LIST pages — read surfaces
 *   - `/w/:ws/agents/:slug` — deciding an Item is a single atomic action and
 *     the ledger shows who decided it afterwards, so a collision is visible
 *     rather than silent
 *   - a shareouts period, `/w/:workspace/walkthrough/:id` — published artifacts, read-only
 *   - `/supervisor`, `/sessions`, `/activity`, `/schedules`,
 *     `/system`, `/settings` — personal or read-only dashboards
 *
 * Order matters — the first match wins, so more specific patterns (a DDD run
 * before its narrative) must come first.
 *
 * `/invite/:token` has no rule here on purpose: a pending invitee has no
 * workspace membership yet, so pageKeyFor returns null and no badge renders
 * — the safe default rather than putting them in a stranger's roster.
 */
export const canopyPresenceRules: RouteRule[] = [
  // Every rule is tenant-scoped (/w/:workspace/...). The server still accepts
  // the `~global` sentinel (`apps/realtime/presence_keys.py`) for a page with
  // no workspace; none has presence today.

  // Live multiplayer chat: a co-edited draft plus a streamed reply, so two
  // people typing into the same session genuinely need to see each other.
  // Each session is its own roster — two people in different chats are not
  // "on the same page" just because both are chatting.
  {
    pattern: /^\/w\/([^/]+)\/chat\/([^/]+)/,
    build: (m) => ({ workspace: m[1], resource: `session:${m[2]}`, subLocation: 'Chat' }),
  },

  // DDD: a specific run (`/ddd/:narrative/:runId`) is its own roster — a
  // distinct rendered/edited state, like a chat session. The narrative page
  // with no run id (`/ddd/:narrative`) is a separate "editor" roster.
  {
    pattern: /^\/w\/([^/]+)\/ddd\/([^/]+)\/([^/]+)/,
    build: (m) => ({ workspace: m[1], resource: `ddd:${m[2]}:${m[3]}`, subLocation: 'Run' }),
  },
  {
    pattern: /^\/w\/([^/]+)\/ddd\/([^/]+)/,
    build: (m) => ({ workspace: m[1], resource: `ddd:${m[2]}`, subLocation: 'Narrative' }),
  },

  // The narrative review surface: a reviewer edits the scene's own text in
  // place, so two reviewers on one review is exactly the silent-clobber case
  // presence exists to warn about. Keyed by its workspace, so only members
  // join the roster; an outside reader on a public link gets no badge.
  {
    pattern: /^\/w\/([^/]+)\/review\/([^/]+)/,
    build: (m) => ({ workspace: m[1], resource: `review:${m[2]}`, subLocation: 'Review' }),
  },
]
