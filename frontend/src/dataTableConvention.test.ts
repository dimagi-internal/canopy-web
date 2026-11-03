import { describe, expect, it } from 'vitest'

/**
 * One table convention (docs/architecture/frontend.md § Tables): a dense header cell is
 * `<Th density="…">` from `components/DataTable`, not a hand-written
 * `<th className="py-1 font-medium">`. Two checks:
 *
 * 1. no raw `<th>` carries one of the standard header combinations (the padding plus
 *    `font-medium` of any `Th` density), anywhere;
 * 2. the files that still hand-write any `<th>` are listed below with how many, a ratchet
 *    that can only shrink: a new raw `<th>` fails, and a migrated one must lower its entry
 *    (or delete it) so the list never goes stale.
 */

// Files still hand-writing `<th>` (headers with their own padding, hidden-at-breakpoint
// cells, sr-only or sortable headers), and how many. Paths relative to `frontend/`.
const NOT_YET_MIGRATED: Record<string, number> = {
  'src/components/agents/AgentClisControl.tsx': 4,
  'src/components/agents/AgentLicenses.tsx': 1,
  'src/components/runners/RunnersTable.tsx': 7,
  'src/components/settings/ActAsYouPanel.tsx': 2,
  'src/components/settings/ConnectedAppsPanel.tsx': 1,
  'src/components/settings/DelegatedCredentialsPanel.tsx': 1,
  'src/components/settings/TokensPanel.tsx': 1,
  'src/components/vault/VaultMembers.tsx': 4,
  'src/components/work/ProjectTable.tsx': 11,
  'src/components/work/TaskTable.tsx': 1,
  'src/dynamicViews/frame/kit.tsx': 1,
  'src/pages/SettingsLayout.tsx': 3,
  'src/pages/WalkthroughsPage.tsx': 7,
  'src/pages/WorkspaceAdmissionRulesPage.tsx': 1,
  'src/pages/WorkspaceAgentAccessPage.tsx': 2,
  'src/pages/WorkspaceAppCustomizePage.tsx': 9,
  'src/pages/WorkspaceEmailPage.tsx': 1,
  'src/pages/WorkspaceRetentionPage.tsx': 1,
  'src/pages/WorkspaceRunnersPage.tsx': 4,
  'src/pages/WorkspaceTaskSettingsPage.tsx': 4,
  'src/pages/WorkspaceVaultsSection.tsx': 4,
  'src/pages/agents/TriggersSection.tsx': 5,
  'src/pages/connectedApps/ConnectionTester.tsx': 3,
  'src/pages/connectedApps/SiteFactsTable.tsx': 3,
  'src/pages/connectedApps/SiteHealthTable.tsx': 6,
  'src/pages/connectedApps/SiteNeedsTable.tsx': 8,
  'src/pages/health/WorkspaceAnalyticsPage.tsx': 4,
  'src/pages/health/WorkspaceHealthPage.tsx': 17,
  'src/pages/runner/RunnerOverview.tsx': 1,
  'src/pages/vault/VaultTabs.tsx': 4,
  'src/pages/work/ProjectMilestones.tsx': 2,
  'src/pages/work/ProjectServices.tsx': 4,
  'src/pages/work/TriagePage.tsx': 9,
}

const raw = import.meta.glob('./**/*.tsx', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
const sources = Object.entries(raw)
  .map(([key, text]) => [`src/${key.slice(2)}`, text] as const)
  .filter(([path]) => path !== 'src/components/DataTable.tsx' && !path.endsWith('.test.tsx'))

const RAW_TH = /<th(?=[\s>])/g
const RAW_TH_CLASS = /<th\s+(?:[^>]*?\s)?className="([^"]*)"/g

// The `Th` densities' class sets; a raw header holding all of one (and no other padding) is a Th.
const STANDARD: string[][] = [
  ['py-1', 'font-medium'],
  ['py-1', 'pr-3', 'font-medium'],
  ['px-3', 'py-2', 'font-medium'],
  ['px-2', 'py-1.5', 'font-medium'],
]

function isStandard(classes: string): boolean {
  const have = classes.split(/\s+/)
  const pad = (t: string) => /^p[xylrtb]?-/.test(t)
  return STANDARD.some((std) => {
    if (!std.every((t) => have.includes(t))) return false
    return !have.filter((t) => !std.includes(t)).some(pad)
  })
}

describe('table convention', () => {
  it('scans the app sources', () => {
    expect(sources.some(([p]) => p.startsWith('src/pages/'))).toBe(true)
  })

  it('has no raw <th> in a standard Th combination', () => {
    const out: string[] = []
    for (const [path, text] of sources) {
      for (const m of text.matchAll(RAW_TH_CLASS)) if (isStandard(m[1])) out.push(`${path}  <th className="${m[1]}">`)
    }
    expect(out).toEqual([])
  })

  it('introduces no new hand-written <th>, and the allowlist only shrinks', () => {
    const counts: Record<string, number> = {}
    for (const [path, text] of sources) {
      const n = (text.match(RAW_TH) ?? []).length
      if (n) counts[path] = n
    }
    const over = Object.entries(counts)
      .filter(([p, n]) => n > (NOT_YET_MIGRATED[p] ?? 0))
      .map(([p, n]) => `${p}: ${n} raw <th> (allowed ${NOT_YET_MIGRATED[p] ?? 0}) — use <Th> from components/DataTable`)
    expect(over).toEqual([])
    const stale = Object.entries(NOT_YET_MIGRATED)
      .filter(([p, n]) => (counts[p] ?? 0) < n)
      .map(([p, n]) => `${p}: lower its entry from ${n} to ${counts[p] ?? 0} (delete it at 0)`)
    expect(stale).toEqual([])
  })

  it('catches what it claims to', () => {
    expect(isStandard('py-1 font-medium')).toBe(true)
    expect(isStandard('px-3 py-2 text-right font-medium')).toBe(true)
    expect(isStandard('py-1.5 pr-3 font-medium')).toBe(false)
    expect(isStandard('py-1 pr-2 font-normal')).toBe(false)
  })
})
