import type { RouteRule } from 'canopy-ui/presence'

/**
 * Workspace segment for pages that are NOT tenant-scoped.
 *
 * The leading `~` is load-bearing, not decoration: the server's page-key
 * parser validates the workspace segment against `^[a-z0-9][a-z0-9_-]{0,63}$`,
 * which `~global` cannot match. A bare `global` CAN match it, which made
 * "global" a workspace name any user could create and then assert to skip
 * the membership gate entirely. Keep this in lockstep with
 * `apps/realtime/presence_keys.py`'s `GLOBAL_SENTINEL` (and with ace-web's,
 * which carries the identical constant).
 */
const GLOBAL_SENTINEL = '~global'

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
 *   - a shareouts period, `/walkthrough/:id` — published artifacts, read-only
 *   - `/supervisor`, `/insights`, `/sessions`, `/activity`, `/schedules`,
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
  // --- Tenant-scoped (/w/:workspace/...) --------------------------------

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

  // --- Global (not tenant-scoped) ---------------------------------------

  // The narrative review surface: a reviewer edits the scene's own text in
  // place, so two reviewers on one narrative is exactly the silent-clobber
  // case presence exists to warn about.
  {
    pattern: /^\/review\/([^/]+)/,
    build: (m) => ({ workspace: GLOBAL_SENTINEL, resource: `review:${m[1]}`, subLocation: 'Review' }),
  },
]
