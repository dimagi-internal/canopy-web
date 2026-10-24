// The agent Settings page's tabs, and which tab each old `settings#anchor`
// address belongs to.
//
// Settings was one long page, so every link into it — the setup checklist's
// steps (apps/agents/setup.py), notices, alert emails, the Google sign-in's
// return — is `settings#<anchor>`. Those addresses are held in mail and
// bookmarks; they keep working by forwarding to the tab that now holds the
// anchor, and the page scrolls to it there.

export type SettingsTab = 'overview' | 'roles' | 'channels' | 'accounts' | 'security'

export const SETTINGS_TABS: { tab: SettingsTab; label: string }[] = [
  { tab: 'overview', label: 'Overview' },
  { tab: 'roles', label: 'Roles' },
  { tab: 'channels', label: 'Channels' },
  { tab: 'accounts', label: 'External accounts' },
  { tab: 'security', label: 'Security' },
]

const ANCHORS: Record<string, SettingsTab> = {
  about: 'overview',
  setup: 'overview',
  delete: 'overview',
  running: 'overview',
  routing: 'overview',
  repository: 'overview',
  operators: 'roles',
  reach: 'channels',
  'cred-github': 'accounts',
  'cred-salesforce': 'accounts',
  'cred-mailbox': 'accounts',
  credentials: 'security',
  secrets: 'security',
  'canopy-login': 'security',
  'cred-vault': 'security',
  'cred-zk': 'security',
  'cred-device-keys': 'security',
  'cred-pat-rotation': 'security',
  'cred-test-vault-key': 'security',
  'cred-alerts': 'security',
}

export function isSettingsTab(s: string | undefined): s is SettingsTab {
  return SETTINGS_TABS.some((t) => t.tab === s)
}

/** The tab an anchor lives on, or null for one this page does not know.
 *  `#credentials` meant "the credentials section"; coming back from the Google
 *  sign-in (`?google=…`) it means the mailbox, which is a connected account. */
export function tabForAnchor(hash: string, search = ''): SettingsTab | null {
  const id = decodeURIComponent(hash.replace(/^#/, ''))
  if (id === 'credentials' && new URLSearchParams(search).has('google')) return 'accounts'
  return ANCHORS[id] ?? null
}

/** The tab a setup step's link (`…/settings#anchor`) opens, if it is on this page. */
export function tabForStepHref(href: string): SettingsTab | null {
  const at = href.indexOf('#')
  if (at < 0 || !/\/agents\/[^/]+\/settings/.test(href.slice(0, at))) return null
  return tabForAnchor(href.slice(at))
}
