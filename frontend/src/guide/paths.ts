/**
 * The five roles in canopy, as a ladder — each contains the one below.
 *
 * Rendered by BOTH the public explainer (`/about`) and the in-app guide
 * (`/guide`), so the public promise cannot drift from the internal docs —
 * `paths.test.ts` asserts every surface referenced here is one the descriptor
 * registry actually describes.
 *
 * This replaced a "five ways in" list of parallel entry points. Roles are the
 * better frame: they ladder, they map onto something the code already enforces
 * (`WorkspaceMembership.ROLE_RANK` is `{viewer: 0, editor: 1, admin: 2, owner: 3}`,
 * and `apps/workspaces/permissions.py` says what each may do), and
 * a reader asking "what can I do here?" is asking about their role, not about
 * which door they came through.
 *
 * One of those five was NOT a role and was lost in the reframe: "you want
 * canopy's skills in your own Claude Code sessions" — mint a token, install
 * the plugin, run no agents at all. It is a real and probably common way to
 * use canopy, so it lives inside `user` (it needs no agent of your own)
 * rather than as a fifth role. `/settings` sits there too, not under
 * `administrator`: minting your OWN access token is something every user
 * does, and filing it under the owner tier told people the opposite.
 *
 * `enforcement` says what the system actually checks for each tier. Since
 * 2026-10-02 every tier is enforced on every surface (a CI test refuses a
 * route that has not declared its gate) — the field stays so the page keeps
 * saying WHAT enforces it, not just that something does.
 */
export interface UserRole {
  id: string
  title: string
  /** Who you are, if this is your role. */
  who: string
  /** What the system actually checks to place you in this tier. */
  enforcement: string
  /** The literal first thing to do. */
  startHere: string
  /** Route paths (from surfaces.ts). Templates, not URLs. */
  surfaces: string[]
  note?: string
}

export const USER_ROLES: UserRole[] = [
  {
    id: 'viewer',
    title: 'Viewer — you were sent a link',
    who: 'An audience. No account, nothing to install, nothing to set up.',
    enforcement: 'No account at all. The only genuinely read-only tier.',
    startHere: 'Open the link. That is the whole of it.',
    surfaces: ['/storyboard/:slug', '/narrative/:slug', '/walkthrough/:id', '/share/:token'],
    note:
      'Storyboards, narratives, demo walkthroughs and shared transcripts are all readable ' +
      'anonymously when shared. This is almost certainly the highest-volume interaction ' +
      'anyone has with canopy.',
  },
  {
    id: 'user',
    title: 'User — you interact with an agent',
    who: 'Someone with work to hand to an agent, or a question waiting on them.',
    enforcement: 'Workspace member. Any role, including viewer.',
    startHere: 'Open Chats in a workspace and start a chat with an agent.',
    surfaces: [
      '/w/:workspace/chat',
      '/w/:workspace/chat/:id',
      '/supervisor',
      '/settings',
      '/system',
    ],
    note:
      'Chat, answer a question an agent is blocked on, decide an item, read the board. ' +
      'Then choose where it runs: a cloud runner needs nothing from you, or pair your own ' +
      'laptop so you can jump into the terminal mid-turn. Or skip agents entirely — mint a ' +
      'token in Settings and install the canopy plugin to use the capability library in ' +
      'your own Claude Code sessions, with no fleet to run.',
  },
  {
    id: 'author',
    title: 'Author — you build and run',
    who: 'Someone shaping what an agent is, not just what it is doing today.',
    enforcement:
      'Editor. Enforced on agents, schedules, turns and on every product surface — ' +
      'walkthroughs, shareouts, reviews, storyboards.',
    startHere: 'Run /canopy:create-agent, then register the agent in your workspace.',
    surfaces: ['/w/:workspace/agents', '/w/:workspace/schedules', '/system'],
    note:
      'Create and edit agents, run turns, publish work products, edit schedules, assign ' +
      'runners. The scaffold is a skeleton — persona and domain skills are yours to write. ' +
      'You read the turns you started; the full turn log is an administrator\'s.',
  },
  {
    id: 'administrator',
    title: 'Administrator — you run the workspace',
    who: 'Whoever keeps the workspace working day to day: who is in it, and what went wrong.',
    enforcement:
      'Admin. Reads every log; manages members and invites below admin; runs the ' +
      'integrations. Holds no keys.',
    startHere: 'Open Activity to read the turn log, or Settings → Members to invite someone.',
    surfaces: ['/w/:workspace/activity', '/w/:workspace/settings',
      '/w/:workspace/settings/members'],
    note:
      'The event log, every turn\'s prompt and transcript, runner drills, connected-site ' +
      'health; inviting, re-roling and removing viewers and editors; inbound mailboxes, ' +
      'Slack sync. Not the shared vault, an agent\'s credentials, the Slack app itself, or ' +
      'deleting the workspace — those are the owner\'s.',
  },
  {
    id: 'owner',
    title: 'Owner — you hold the keys',
    who: 'Whoever decides who else gets in at any level, and holds the keys.',
    enforcement: 'Owner. Enforced throughout workspace admin and on agent credentials.',
    startHere: 'Open Settings → Secrets for the shared vault, or an agent\'s Credentials.',
    surfaces: ['/w/:workspace/settings', '/w/:workspace/settings/members',
      '/w/:workspace/settings/secrets'],
    note:
      'Everything an administrator does, plus making admins and owners, Slack, connected '
      + 'sites, secrets. Secrets has the SHARED vault every agent here reads (the '
      + 'Google OAuth clients, the GitHub token); an agent\'s OWN vault sits on the agent, '
      + 'under Settings → Credentials. Both are owner-only: they are the keys a runner '
      + 'resolves everything else from. 1Password holds the secrets; canopy-web holds the '
      + 'service accounts that open them.',
  },
]
