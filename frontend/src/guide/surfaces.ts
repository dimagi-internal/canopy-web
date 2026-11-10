/**
 * One descriptor per documented surface: the registry of pages, as DATA.
 *
 * The page itself says what it shows and does; a descriptor holds only what a
 * reader cannot see from the page: where the page lives (`home`, `group`,
 * `flat`), its name, and when to open it (`use`). Never restate a page's
 * contents, its labels or who may change what (a role gate is code on the page
 * and the server): prose that restates them drifts.
 *
 * Data, not prose in a wiki, because tests read it and the guide renders it.
 * guide/coverage.test.ts asserts this file covers every route and names no
 * route that does not exist; guide/surfaces.test.ts keeps `use` to one line
 * free of role gates and page contents.
 *
 * Everything else that sorts pages reads it from here: the guide's grouping
 * (`group`, `home`), the workspace switcher's fallback (`home`) and the tenancy
 * test (`flat`). A route that is not a page (a redirect, a catch-all) is in
 * coverage.ts's NOT_PAGES instead, with its own reason.
 *
 * `path` MUST match the route path in router.tsx character for character.
 *
 * Quote a UI label or control name you mention in double quotes (“Keep a copy
 * in canopy”): labels.test.tsx fails unless the described page's source
 * contains it. Unquoted words are description and are not checked.
 */
export interface SurfaceDescriptor {
  /** Exactly the route path from routeTable. */
  path: string
  /** The page a reader reaches this one from, when no menu links it: the guide
   *  files it under that page's menu, and the workspace switcher lands there
   *  when this page's section has no page of its own. */
  home?: string
  /** The guide's heading for a page in no menu at all. Wins over the menus. */
  group?: string
  /** Why this page lives outside /w/:workspace — required there, and only there
   *  (routeTenancy.test.ts). */
  flat?: string
  title: string
  /** When to open this page, one sentence: the situation, not the contents and
   *  not who may change what. */
  use: string
}

export const SURFACES: SurfaceDescriptor[] = [
  // --- Personal / global (not tenant-scoped) ---
  {
    path: '/system',
    flat: 'personal/global: the plugin catalog, read from the canopy plugin — no tenant rows',
    title: 'Capabilities',
    use: 'When you want to know what skills, agents and commands canopy can give an agent.',
  },
  {
    path: '/guide',
    flat: 'personal/global: the self-documenting route registry',
    title: 'Guide',
    use: 'When you are looking for the page that does something.',
  },
  {
    path: '/get-started',
    flat: 'personal/global: the public getting-started guide (personalised when signed in)',
    title: 'Get started',
    use: 'Your first stop with canopy agents; it works before you have an account.',
  },
  {
    path: '/sessions',
    flat: 'personal/global: YOUR shared transcripts, across your workspaces',
    title: 'Shared transcripts',
    use: 'When you want to hand someone a transcript, or manage the ones you already shared.',
  },
  {
    path: '/people/me',
    home: '/settings',
    flat: 'personal/global: what agents know about you, the subject',
    title: 'Your memory',
    use: 'When you want to see or control what agents know about you.',
  },
  {
    path: '/people/:personId/page',
    group: 'A memory page shared with an agent',
    flat: 'personal/global: a person\'s own memory page, read-only, for an agent they let see it',
    title: 'Someone’s memory page',
    use: 'When a person has let your agent see their memory page.',
  },
  {
    path: '/hcp-clients',
    group: 'The HCP service (administrators)',
    flat: 'global: the HCP service\'s client registry spans every person, not a workspace (superusers)',
    title: 'HCP service clients',
    use: 'When an application outside canopy needs approval to ask people for access to their memory.',
  },
  {
    path: '/home',
    flat: 'personal/global: what is waiting on you across every workspace you are in — read once per organisation and shown side by side',
    title: 'Home',
    use: 'Your front page: what is waiting on you across every workspace.',
  },
  {
    path: '/supervisor',
    flat: 'personal/global: the cross-fleet Waiting on you — the fleet spans workspaces',
    title: 'Supervisor',
    use: 'When you own agents and want the whole fleet at a glance, on a phone or the menu bar.',
  },
  {
    path: '/ide',
    flat: 'personal/global: the desktop session IDE — your sessions across every workspace and runner',
    title: 'IDE',
    use: 'When you run sessions on several runners or computers and want them in one tree.',
  },
  {
    path: '/ide/:id',
    flat: 'personal/global: one session open in the IDE; the session itself names its workspace',
    title: 'IDE — one session',
    use: 'When you want one session open beside the IDE tree.',
  },
  {
    path: '/schedules',
    home: '/home',
    flat: 'personal/global: every schedule across your workspaces (tenant twin: /w/:workspace/schedules)',
    title: 'Schedules (all workspaces)',
    use: 'When you want to see what recurring agent work will run, across your workspaces.',
  },
  {
    path: '/activity',
    home: '/home',
    flat: 'personal/global: the fleet turn log across your workspaces (tenant twin: /w/:workspace/activity)',
    title: 'Activity (all workspaces)',
    use: 'When you want to check whether a triggered turn actually ran, across your workspaces.',
  },
  {
    path: '/settings',
    flat: 'personal/global: your own account, the same in every workspace',
    title: 'Settings',
    use: 'When you want to change something about you, the same in every workspace.',
  },
  {
    path: '/settings/:section',
    home: '/settings',
    flat: 'personal/global: one section of your settings',
    title: 'Settings — one section',
    use: 'When a link names one section of your settings.',
  },
  {
    path: '/new-workspace',
    group: 'Your first workspace',
    flat: 'pre-tenant: the create-a-workspace form',
    title: 'Create a workspace',
    use: 'When you have no workspace yet, or want another.',
  },

  // --- Public viewers (under their workspace; self-enforce visibility) ---
  {
    path: '/w/:workspace/walkthrough/:id',
    home: '/w/:workspace/walkthroughs',
    group: 'Shared by link',
    title: 'Walkthrough viewer',
    use: 'When someone sent you a link to a recorded demo.',
  },
  {
    path: '/w/:workspace/connections/:id/connected',
    home: '/w/:workspace/settings/connections',
    title: 'Connected',
    use: 'Where a sign-in to Salesforce or Google lands.',
  },
  {
    path: '/w/:workspace/connections/:id/share',
    home: '/w/:workspace/settings/connections',
    title: 'Share this connection',
    use: 'When you want agents or another computer to use a connection you made.',
  },
  {
    path: '/w/:workspace/review/:id',
    home: '/w/:workspace/agents',
    group: 'Shared by link',
    title: 'Review link (forwards to the storyboard)',
    use: 'When someone sent you a link to a narrative review.',
  },
  {
    path: '/beta-requests',
    group: 'Requests for access to Canopy',
    flat: 'personal/global: requests for access to Canopy itself name no workspace',
    title: 'Requests for access to Canopy',
    use: 'When you receive requests for access to canopy and want to see them all.',
  },
  {
    path: '/beta-requests/:requestId',
    group: 'Requests for access to Canopy',
    flat: 'personal/global: one request for access to Canopy itself',
    title: 'Request for access to Canopy',
    use: 'When the notification email links you to one request for access.',
  },
  {
    path: '/invite/:token',
    group: 'Shared by link',
    flat: 'pre-tenant: an invitee has no membership yet, so there is no tenant to scope it under',
    title: 'Accept invite',
    use: 'When you were invited to a workspace and have the link.',
  },
  {
    path: '/pair/:code',
    group: 'Approving a new runner',
    flat: 'pre-tenant: a runner pairing has no workspace until the person approving it picks one',
    title: 'Pair a computer as your runner',
    use: 'Right after installing Canopy Runner, which opens this page.',
  },
  {
    path: '/listen',
    group: 'Recording a meeting from your phone',
    flat: 'personal/global: your phone and your own laptop, in no workspace',
    title: 'Canopy Listen on your phone',
    audience: 'Someone recording a meeting on their phone for their laptop to transcribe',
    what: "Record a meeting on this phone; your own laptop, running Canopy Listen, transcribes it and your agent takes notes. The audio is encrypted on the phone and only that laptop can open it: Canopy carries ciphertext and never stores it. Pair the phone once by scanning the QR code from “Pair a phone” on the laptop's own page. Then the page shows the laptop and whether it is online, a notice to acknowledge before the first recording, and a record button; while recording, a timer, a level meter and the live transcript with each speaker in their own colour (tap one to rename them), “End meeting” to save and “Discard” to throw it away.",
    needsFirst: 'Canopy Listen running on your laptop, and your laptop online as your runner',
    actions: ['Pair this phone from the laptop\'s QR code', 'Record a meeting', 'Rename a speaker', 'End and save, or discard', 'Unpair this phone'],
  },
  {
    path: '/act-as/:requestId',
    group: 'Letting an agent act as you',
    flat: 'personal/global: a request to act as YOU, from an agent in any of your workspaces',
    title: 'Let an agent act as you on canopy',
    use: 'When an agent in a turn you started asks to use canopy as you.',
  },
  {
    path: '/confirm/page-permission/:grantId',
    group: 'Letting an agent act as you',
    flat: 'personal/global: a permission to act as YOU, on a page of any site you use',
    title: 'Confirm a page’s act-as-you permission',
    use: 'When a page panel sends you to the browser to confirm an the agent’s access.',
  },

  // --- Tenant-scoped surfaces under /w/:workspace ---
  {
    path: '/w/:workspace/settings',
    title: 'Workspace settings',
    use: 'When you want to configure the workspace.',
  },
  {
    path: '/w/:workspace/settings/members',
    title: 'Members',
    use: 'When you want to see who is in the workspace or invite someone.',
  },
  {
    path: '/w/:workspace/settings/member-tree',
    home: '/w/:workspace/settings/members',
    title: 'Members across workspaces',
    use: 'When you want everyone across this workspace and the ones below it in one table.',
  },
  {
    path: '/w/:workspace/members/:userId',
    home: '/w/:workspace/settings/members',
    title: 'A member',
    use: 'When you want one member of the workspace.',
  },
  {
    path: '/w/:workspace/settings/access-requests',
    title: 'Access requests',
    use: 'When people have asked to be invited to the workspace.',
  },
  {
    path: '/w/:workspace/settings/access-requests/:requestId',
    title: 'Access request',
    use: 'When the notification email links you to one request to join.',
  },
  {
    path: '/w/:workspace/runner-invitations/:invitationId',
    home: '/w/:workspace/agents',
    group: 'Approving a new runner',
    title: 'Approve a new cloud runner',
    use: 'When a new cloud box asks to pair and its setup script gave you this link.',
  },
  {
    path: '/w/:workspace/sign-offs/:signOffId',
    home: '/w/:workspace/agents',
    group: 'Approving a site sign-off',
    title: 'Approve a site sign-off',
    use: 'When a host site asks you to approve an action.',
  },
  {
    path: '/w/:workspace/settings/host-sites',
    title: 'Host sites',
    use: 'When you embed one of this workspace\'s agents in a website.',
  },
  {
    path: '/w/:workspace/settings/connections',
    title: 'Connections',
    use: 'When you set up how the workspace connects Salesforce and Google.',
  },
  {
    path: '/w/:workspace/settings/secrets',
    title: 'Secrets',
    use: 'When you want to know where this workspace keeps its secrets.',
  },
  {
    path: '/w/:workspace/settings/retention',
    title: 'Retention',
    use: 'When you decide how long conversation content is kept.',
  },
  {
    path: '/w/:workspace/settings/tasks',
    title: 'Task settings',
    use: 'When you want the workspace\'s tasks to read differently.',
  },
  {
    path: '/w/:workspace/settings/admission-rules',
    title: 'Admission rules',
    use: 'When you decide which turns are refused before they run.',
  },
  {
    path: '/w/:workspace/settings/roles',
    title: 'Agent roles',
    use: 'When you decide what people may do with the workspace\'s agents.',
  },
  {
    path: '/w/:workspace/settings/inbound',
    title: 'Inbound push',
    use: 'When you set up email that wakes an agent.',
  },
  {
    path: '/w/:workspace/settings/email',
    title: 'Email',
    use: 'When you bring your own domain\'s email to canopy.',
  },
  {
    path: '/w/:workspace/health',
    title: 'Health',
    use: 'When something seems off and you want to know what is not set up right.',
  },
  {
    path: '/w/:workspace/analytics',
    home: '/w/:workspace/health',
    title: 'Analytics',
    use: 'When you want to know how the workspace is used.',
  },
  {
    path: '/w/:workspace/network',
    home: '/w/:workspace/health',
    title: 'Network',
    use: 'When you want to see who works with whom.',
  },
  {
    path: '/w/:workspace/settings/topology',
    title: 'Fleet map',
    use: 'When you want the fleet as one picture.',
  },
  {
    path: '/w/:workspace/settings/topology/runners',
    title: 'Runner topology',
    use: 'When you want to know which runner each agent\'s turns land on.',
  },
  {
    path: '/w/:workspace/runners/:runnerId',
    home: '/w/:workspace/settings/topology/runners',
    title: 'Runner',
    use: 'When you want one runner and everything you can do with it.',
  },
  {
    path: '/w/:workspace/runners/:runnerId/:tab',
    home: '/w/:workspace/settings/topology/runners',
    title: 'Runner tab',
    use: 'When a link names one tab of a runner.',
  },
  {
    path: '/w/:workspace/vaults/:vaultId',
    home: '/w/:workspace/settings/secrets',
    title: 'Vault',
    use: 'When you want one workspace vault.',
  },
  {
    path: '/w/:workspace/vaults/:vaultId/:tab',
    home: '/w/:workspace/settings/secrets',
    title: 'Vault tab',
    use: 'When a link names one tab of a vault.',
  },
  {
    path: '/w/:workspace/settings/topology/agents',
    title: 'Agent topology',
    use: 'When you want to know which agent can send which other agent work.',
  },
  {
    path: '/w/:workspace/settings/slack',
    title: 'Slack',
    use: 'When you connect the workspace to Slack.',
  },
  {
    path: '/w/:workspace/timeline',
    title: 'Timeline',
    use: 'When you want to follow what happened across the workspace\'s apps.',
  },
  {
    path: '/w/:workspace/shareouts',
    title: 'Shareouts',
    use: 'When you want to catch up on what the team shipped.',
  },
  {
    path: '/w/:workspace/shareouts/:period',
    title: 'Shareout (one period)',
    use: 'When someone links you to one period\'s briefing.',
  },
  {
    path: '/w/:workspace/walkthroughs',
    title: 'Walkthroughs',
    use: 'When you are looking for a recorded demo.',
  },
  {
    path: '/w/:workspace/storyboards',
    title: 'Storyboards',
    use: 'When you want to assemble demos into one shareable link, or find one.',
  },
  {
    path: '/w/:workspace/views',
    title: 'Views',
    use: 'When you want the screens generated for the workspace.',
  },
  {
    path: '/w/:workspace/apps',
    title: 'Apps',
    use: 'When you want the apps the workspace runs.',
  },
  {
    path: '/w/:workspace/apps/store',
    home: '/w/:workspace/apps',
    title: 'App in the store',
    use: 'When you are deciding whether to install an app from the catalog.',
  },
  {
    path: '/w/:workspace/meetings',
    title: 'Meetings',
    use: 'When you want to find or add meeting notes.',
  },
  {
    path: '/w/:workspace/meetings/:id',
    home: '/w/:workspace/meetings',
    title: 'Meeting',
    use: 'When you want one meeting.',
  },
  {
    path: '/w/:workspace/apps/personal/:key',
    home: '/w/:workspace/apps',
    title: 'Personal app',
    use: 'When you are deciding whether to install an unreleased app for yourself, or already have.',
  },
  {
    path: '/w/:workspace/apps/personal/review/:id',
    home: '/w/:workspace/apps',
    title: 'Personal app submission',
    use: 'When you review a version submitted for the store, or submitted it.',
  },
  {
    path: '/w/:workspace/apps/:appKey/:page?',
    home: '/w/:workspace/apps',
    title: 'App',
    use: 'When you use an app the workspace runs.',
  },
  {
    path: '/w/:workspace/apps/:appKey/customize',
    home: '/w/:workspace/apps',
    title: 'Customize an app',
    use: 'When you want to change an installed app without a developer.',
  },
  {
    path: '/w/:workspace/app/:appKey/:page?',
    home: '/w/:workspace/apps',
    title: 'Public app page',
    use: 'When an app publishes a page for the public.',
  },
  {
    path: '/w/:workspace/app/:appKey/l/:token',
    home: '/w/:workspace/apps',
    title: 'Private app link',
    use: 'When an app emailed you a signed link to one record.',
  },
  {
    path: '/w/:workspace/views/:id',
    home: '/w/:workspace/views',
    title: 'View',
    use: 'When you want to open one generated screen.',
  },
  {
    path: '/w/:workspace/agents',
    title: 'Agents',
    use: 'When you want to see the workspace\'s agents.',
  },
  {
    path: '/w/:workspace/tasks',
    title: 'Tasks',
    use: 'When you want to see the work, yours or anyone\'s.',
  },
  {
    path: '/w/:workspace/mail',
    title: 'Mail',
    use: 'When you read or send mail from your address on the workspace\'s domain.',
  },
  {
    path: '/w/:workspace/tasks/:taskKey',
    home: '/w/:workspace/tasks',
    title: 'Task',
    use: 'When you want one task.',
  },
  {
    path: '/w/:workspace/approvals/:approvalId',
    home: '/w/:workspace/tasks',
    title: 'Approval request',
    use: 'When a push, message, email or alert asks you to approve something.',
  },
  {
    path: '/w/:workspace/triage',
    title: 'Triage',
    use: 'When you decide what agents suggested or people proposed.',
  },
  {
    path: '/w/:workspace/projects',
    title: 'Projects',
    use: 'When you want to see the workspace\'s projects.',
  },
  {
    path: '/w/:workspace/projects/:handle',
    home: '/w/:workspace/projects',
    title: 'Project',
    use: 'When you want one project.',
  },
  {
    path: '/w/:workspace/projects/:handle/kb',
    home: '/w/:workspace/projects',
    title: 'Knowledge base',
    use: 'When you want to see where a project\'s knowledge base comes from and how it changed.',
  },
  {
    path: '/w/:workspace/mcp',
    home: '/w/:workspace/projects',
    title: 'MCP servers',
    use: 'When you want to add a workspace project\'s tools as an MCP server.',
  },
  {
    path: '/w/:workspace/agents/new',
    title: 'New agent',
    use: 'When you create an agent.',
  },
  {
    path: '/w/:workspace/schedules',
    title: 'Schedules',
    use: 'When you want to see this workspace\'s recurring schedules.',
  },
  {
    path: '/w/:workspace/sessions',
    title: 'Sessions',
    use: 'When you want to pick up a chat or runner session from any device.',
  },
  {
    path: '/w/:workspace/sessions/:id',
    title: 'One chat',
    use: 'When you talk with an agent.',
  },
  {
    path: '/w/:workspace/activity',
    title: 'Activity',
    use: 'When you want to check whether a triggered turn in this workspace ran.',
  },
  {
    path: '/w/:workspace/huddles',
    title: 'Huddles',
    use: 'When you follow how the fleet works as a team.',
  },
  {
    path: '/w/:workspace/huddles/:id',
    title: 'Huddle',
    use: 'When you want one huddle.',
  },
  {
    path: '/w/:workspace/threads/:id',
    home: '/w/:workspace/huddles',
    title: 'Agent conversation',
    use: 'When you follow a conversation between agents.',
  },
  {
    path: '/w/:workspace/agents/:slug/projects',
    group: 'Agent workspace',
    title: 'Agent projects',
    use: 'Where an agent opens: its projects.',
  },
  {
    path: '/w/:workspace/agents/:slug/projects/:ref',
    group: 'Agent workspace',
    title: 'Project (agent link)',
    use: 'When an old or agent-board link names a project.',
  },
  {
    path: '/w/:workspace/agents/:slug/projects/:ref/people/:person',
    group: 'Agent workspace',
    title: 'Contributor page',
    use: 'When you want one person\'s page on one project.',
  },
  {
    path: '/w/:workspace/agents/:slug/tasks',
    group: 'Agent workspace',
    title: 'Agent tasks',
    use: 'When you want this agent\'s tasks.',
  },
  {
    path: '/w/:workspace/agents/:slug/turns',
    group: 'Agent workspace',
    title: 'Agent turns',
    use: 'When you want to know what this agent has done.',
  },
  {
    path: '/w/:workspace/agents/:slug/huddles',
    group: 'Agent workspace',
    title: 'Agent huddles',
    use: 'When you want the huddles this agent took part in.',
  },
  {
    path: '/w/:workspace/agents/:slug/triggers',
    group: 'Agent workspace',
    title: 'Agent triggers',
    use: 'When you want to know what starts this agent on its own.',
  },
  {
    path: '/w/:workspace/agents/:slug/mail',
    group: 'Agent workspace',
    title: 'Agent mail',
    use: 'When you want the email threads this agent sent and received.',
  },
  {
    path: '/w/:workspace/agents/:slug/skills',
    group: 'Agent workspace',
    title: 'Agent skills',
    use: 'When you want the skills this agent has.',
  },
  {
    path: '/w/:workspace/agents/:slug/skills/history',
    group: 'Agent workspace',
    title: 'Agent skill history',
    use: 'When you want to see how this agent\'s skills changed over time.',
  },
  {
    path: '/w/:workspace/agents/:slug/capabilities',
    group: 'Agent workspace',
    title: 'Cloud capabilities',
    use: 'When you want the cloud capabilities this agent declares and whether they are granted.',
  },
  {
    path: '/w/:workspace/agents/:slug/connect/google',
    group: 'Agent workspace',
    title: 'Connect your Google account',
    use: 'When you connect your own Google account for the agent to use on your behalf.',
  },
  {
    path: '/w/:workspace/agents/:slug/settings/:tab',
    group: 'Agent workspace',
    title: 'Agent settings',
    use: 'When you configure this agent.',
  },
  {
    path: '/w/:workspace/ddd',
    title: 'DDD narratives',
    use: 'When you build or review a demo.',
  },
  {
    path: '/w/:workspace/ddd/:narrative',
    title: 'DDD narrative',
    use: 'When you want one narrative and its runs.',
  },
  {
    path: '/w/:workspace/ddd/:narrative/:runId',
    title: 'DDD run package',
    use: 'When you want one run\'s full package.',
  },

  // --- Public, chrome-less routes (mounted outside the app shell) ---
  {
    path: '/w/:workspace/share/:token',
    home: '/w/:workspace/agents',
    group: 'Shared by link',
    title: 'Shared session (public)',
    use: 'When someone sent you a link to a shared transcript.',
  },
  {
    path: '/w/:workspace/ddd-release/:narrative/:runId',
    home: '/w/:workspace/ddd',
    group: 'Shared by link',
    title: 'DDD run release (public)',
    use: 'When someone sent you a link to a finished demo.',
  },
  {
    path: '/w/:workspace/storyboard/:slug',
    home: '/w/:workspace/storyboards',
    group: 'Shared by link',
    title: 'Storyboard (public)',
    use: 'When someone sent you a link to a storyboard.',
  },
  {
    path: '/w/:workspace/narrative/:slug',
    home: '/w/:workspace/storyboards',
    group: 'Shared by link',
    title: 'Narrative (public)',
    use: 'When someone sent you a link to a narrative.',
  },
  {
    path: '/about',
    group: 'The public site',
    flat: 'personal/global: the public explainer',
    title: 'About Canopy (public)',
    use: 'The page to send someone outside the team who asks what canopy is.',
  },
]

export function describedPaths(): Set<string> {
  return new Set(SURFACES.map((s) => s.path))
}
