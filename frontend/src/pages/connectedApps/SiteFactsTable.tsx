import { useState, type JSX } from 'react'
import { Button, Input } from 'canopy-ui/ui'
import { discoverSite, type ConnectedApp, type SiteDiscovery, type SiteFacts } from '@/api/connectedApps'
import { Td, Th, Tr } from '@/components/DataTable'

const FACT_ROWS: { key: keyof SiteFacts; label: string }[] = [
  { key: 'origins', label: 'Site URLs' },
  { key: 'jwks_url', label: 'Signing keys URL' },
  { key: 'issuer', label: 'Sign-in issuer' },
  { key: 'mcp_resource', label: 'MCP server' },
]

function factText(facts: SiteFacts, key: keyof SiteFacts): string {
  const v = facts[key]
  return (typeof v === 'string' ? v : v.join(', ')) || '—'
}

/** How canopy believed the document, in words: an owner adopting keys should
 *  know when the keys come from the document itself. */
export function discoverySource(d: Pick<SiteDiscovery, 'url' | 'signed_by' | 'registered_keys_verify'>): string {
  const how =
    d.signed_by === 'registered_keys'
      ? 'signed with the keys canopy already has for this site'
      : d.registered_keys_verify === false
        ? 'NOT signed with the keys canopy has for this site — only with the keys it names; saving adopts those keys'
        : 'signed with the keys it names'
  return `Read from ${d.url}, ${how}.`
}

/** What the site says about itself, beside what canopy holds now (for a
 *  registered site). One row per fact; a changed row is marked. */
export function SiteFactsTable({ discovery }: { discovery: SiteDiscovery }): JSX.Element {
  const changed = new Set(discovery.changes ?? [])
  const current = discovery.current
  return (
    <table className="w-full border-collapse text-xs">
      <thead>
        <Tr rule="bottom" head="plain">
          <Th density="compact-gutter" weight="normal">Setting</Th>
          {current && <Th density="compact-gutter" weight="normal">In canopy now</Th>}
          <Th density="compact-gutter" weight="normal">The site says</Th>
        </Tr>
      </thead>
      <tbody>
        {FACT_ROWS.map(({ key, label }) => (
          <Tr rule="bottom-last" top key={key}>
            <Td density="compact-gutter" tone="foreground">
              {label}
              {changed.has(key) && <span className="ml-1 text-warning">changes</span>}
            </Td>
            {current && (
              <Td density="compact-gutter" tone="muted" mono className="break-all text-[11px]">
                {factText(current, key)}
              </Td>
            )}
            <Td density="compact-gutter" tone="foreground" mono className="break-all text-[11px]">
              {factText(discovery.site, key)}
            </Td>
          </Tr>
        ))}
      </tbody>
    </table>
  )
}

/** The values to save when an owner adopts what a site declares. */
export type AdoptedFacts = {
  origins: string[]
  jwks_url: string
  host_issuer: string
  host_mcp_resource: string
}

export function adoptedFacts(site: SiteFacts): AdoptedFacts {
  return {
    origins: [...site.origins],
    jwks_url: site.jwks_url,
    host_issuer: site.issuer,
    host_mcp_resource: site.mcp_resource,
  }
}

/**
 * "Fill from the site's own documents" on a registered site: read its signed
 * `site.json`, show what it declares beside what canopy holds, and save only
 * when the owner says so — through the ordinary, audited update. A site never
 * changes its own trusted origins by publishing a document.
 */
export function FillFromSite({
  slug,
  app,
  busy,
  onSave,
}: {
  slug: string
  app: Pick<ConnectedApp, 'id'>
  busy: boolean
  onSave: (facts: AdoptedFacts) => void
}): JSX.Element {
  const [found, setFound] = useState<SiteDiscovery | null>(null)
  const [reading, setReading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onRead() {
    setReading(true)
    setError(null)
    try {
      setFound(await discoverSite(slug, { appId: app.id }))
    } catch (e) {
      setFound(null)
      setError(e instanceof Error ? e.message : "Could not read the site's own documents.")
    } finally {
      setReading(false)
    }
  }

  const changes = found?.changes ?? []
  return (
    <div className="space-y-2 border-t border-border pt-2">
      <div className="flex items-center gap-3">
        <Button size="sm" variant="outline" disabled={reading || busy} onClick={() => void onRead()}>
          {reading ? 'Reading…' : "Fill from the site's own documents"}
        </Button>
        <span className="text-xs text-muted-foreground">
          Reads the URLs, keys, issuer and MCP server the site publishes in its signed site.json. Nothing
          changes until you save.
        </span>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
      {found && (
        <div className="space-y-2">
          <p className={`text-xs ${found.signed_by === 'registered_keys' ? 'text-muted-foreground' : 'text-warning'}`}>
            {discoverySource(found)}
          </p>
          <SiteFactsTable discovery={found} />
          {changes.length === 0 ? (
            <p className="text-xs text-success">canopy already has what the site says.</p>
          ) : (
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                disabled={busy}
                onClick={() => {
                  onSave(adoptedFacts(found.site))
                  setFound(null)
                }}
              >
                Save these values
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setFound(null)}>
                Cancel
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * The first step of connecting a site: type its address, and canopy reads the
 * rest from the site's signed site.json into the form below for the owner to
 * check before connecting.
 */
export function SiteAddressFill({
  slug,
  onFill,
}: {
  slug: string
  onFill: (found: SiteDiscovery) => void
}): JSX.Element {
  const [address, setAddress] = useState('')
  const [reading, setReading] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function onRead() {
    setReading(true)
    setError(null)
    setNote(null)
    try {
      const found = await discoverSite(slug, { address: address.trim() })
      onFill(found)
      setNote(`${discoverySource(found)} Check the values below, then Connect.`)
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not read the site's own documents.")
    } finally {
      setReading(false)
    }
  }

  return (
    <div className="space-y-1">
      <span className="text-xs text-foreground-secondary">Site address</span>
      <div className="flex gap-2">
        <Input
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          placeholder="https://labs.connect.dimagi.com"
          aria-label="Site address"
        />
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={reading || !address.trim()}
          onClick={() => void onRead()}
        >
          {reading ? 'Reading…' : 'Fill from the site'}
        </Button>
      </div>
      <span className="block text-xs text-muted-foreground">
        A site on dimagi-canopy 0.16+ publishes its URLs, keys, issuer and MCP server; canopy fills them in
        for you to check.
      </span>
      {note && <p className="text-xs text-muted-foreground">{note}</p>}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  )
}
