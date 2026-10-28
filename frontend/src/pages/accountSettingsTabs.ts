// Your account settings' tabs (`/settings/<tab>`), and which tab each old
// `/settings#anchor` or `/settings?…` address belongs to.
//
// Settings was one long page, so every link into it — a setup step, a step-up
// prompt, an alert, a runner's approve link, the GitHub sign-in's return — is
// `/settings#<anchor>` or `/settings?<param>`. Those addresses are held in mail,
// in runner output and in bookmarks; they keep working by forwarding to the tab
// that now holds the anchor, and the page scrolls to it there.

export type AccountSettingsTab = 'account' | 'security' | 'agents' | 'runners' | 'connections'

export const ACCOUNT_SETTINGS_TABS: { tab: AccountSettingsTab; label: string }[] = [
  { tab: 'account', label: 'Account' },
  { tab: 'security', label: 'Security' },
  { tab: 'agents', label: 'Agents acting for you' },
  { tab: 'runners', label: 'Runners' },
  { tab: 'connections', label: 'Connections' },
]

const ANCHORS: Record<string, AccountSettingsTab> = {
  'contact-details': 'account',
  security: 'security',
  you: 'security',
  passkeys: 'security',
  'vault-key': 'security',
  'device-keys': 'security',
  'your-runners': 'security',
  'your-machines': 'security',
  'your-agents': 'security',
  'canopy-projects': 'runners',
}

// A query parameter a link carries to the panel that reads it.
const PARAMS: [string, AccountSettingsTab][] = [
  ['device_key', 'security'],
  ['github', 'connections'],
]

export function isAccountSettingsTab(s: string | undefined): s is AccountSettingsTab {
  return ACCOUNT_SETTINGS_TABS.some((t) => t.tab === s)
}

/** The tab an old address means, or null for one that names no tab. The anchor
 *  decides first; a query parameter only when there is no known anchor. */
export function tabForAddress(hash: string, search = ''): AccountSettingsTab | null {
  const id = decodeURIComponent(hash.replace(/^#/, ''))
  if (ANCHORS[id]) return ANCHORS[id]
  const params = new URLSearchParams(search)
  return PARAMS.find(([p]) => params.has(p))?.[1] ?? null
}
