/**
 * The four roles in canopy, as a ladder — each contains the one below.
 *
 * Rendered by BOTH the public explainer (`/about`) and the in-app guide
 * (`/guide`), so the public promise cannot drift from the internal docs —
 * `paths.test.ts` asserts every surface referenced here is one the descriptor
 * registry actually describes.
 *
 * This replaced a "five ways in" list of parallel entry points. Roles are the
 * better frame: they ladder, they map onto something the code already enforces
 * (`WorkspaceMembership.ROLE_RANK` is `{viewer: 0, editor: 1, owner: 2}`), and
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
 * IMPORTANT, and the reason `enforcement` exists as a field: these tiers are
 * fully enforced on the AGENTS and HARNESS surfaces (agents, schedules, turns)
 * and only partly enforced elsewhere. Projects, walkthroughs, shareouts and
 * reviews check that you are in the tenant, not what role you hold. Saying so
 * in the data keeps the rendered page honest rather than aspirational — see
 * docs/architecture/roles.md.
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
      'Editor on agents, schedules and turns. Membership-only on projects, ' +
      'walkthroughs, shareouts and reviews — a known gap.',
    startHere: 'Run /canopy:create-agent, then register the agent in your workspace.',
    surfaces: ['/w/:workspace/agents', '/w/:workspace/schedules', '/system'],
    note:
      'Create and edit agents, run turns, publish work products, edit schedules, assign ' +
      'runners. The scaffold is a skeleton — persona and domain skills are yours to write.',
  },
  {
    id: 'administrator',
    title: 'Administrator — you own the workspace',
    who: 'Whoever decides who else gets in, and holds the keys.',
    enforcement: 'Owner. Enforced throughout workspace admin and on agent credentials.',
    startHere: 'Open Members to invite someone, or an agent\'s Credentials tab to hold its keys.',
    surfaces: ['/w/:workspace/members', '/w/:workspace/inbound'],
    note:
      'Members and invites, agent credentials, the shared vault, inbound configuration. ' +
      'Credentials are owner-only because they are the keys a runner resolves everything ' +
      'else from.',
  },
]
