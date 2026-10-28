// The sections of Settings (`/settings/<section>`, down its left rail), and which
// section each old `/settings#anchor` or `/settings?…` address belongs to.
//
// Settings was one long page, so every link into it — a setup step, a step-up
// prompt, an alert, a runner's approve link, the GitHub sign-in's return — is
// `/settings#<anchor>` or `/settings?<param>`. Those addresses are held in mail,
// in runner output and in bookmarks; they keep working by forwarding to the
// section that now holds the anchor, and the page scrolls to it there.

export type SettingsSection = 'profile' | 'account' | 'security' | 'agents' | 'runners' | 'connections'

export const SETTINGS_SECTIONS: { section: SettingsSection; label: string }[] = [
  { section: 'profile', label: 'Profile' },
  { section: 'account', label: 'Account' },
  { section: 'security', label: 'Security' },
  { section: 'agents', label: 'Agents acting for you' },
  { section: 'runners', label: 'Runners' },
  { section: 'connections', label: 'Connections' },
]

const ANCHORS: Record<string, SettingsSection> = {
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
const PARAMS: [string, SettingsSection][] = [
  ['device_key', 'security'],
  ['github', 'connections'],
]

export function isSettingsSection(s: string | undefined): s is SettingsSection {
  return SETTINGS_SECTIONS.some((t) => t.section === s)
}

/** The section an old address means, or null for one that names none. The anchor
 *  decides first; a query parameter only when there is no known anchor. */
export function sectionForAddress(hash: string, search = ''): SettingsSection | null {
  const id = decodeURIComponent(hash.replace(/^#/, ''))
  if (ANCHORS[id]) return ANCHORS[id]
  const params = new URLSearchParams(search)
  return PARAMS.find(([p]) => params.has(p))?.[1] ?? null
}
