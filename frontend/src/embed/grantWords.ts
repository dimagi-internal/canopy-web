import { siteLabel } from './siteLabel'

/** What a permission prompt can say about the site, from the grant canopy sends. */
export interface GrantWords {
  /** The site's registered name (a slug). */
  site: string
  /** The raw scopes the page was granted (space-separated). */
  scope?: string
  /** The site's own display name, from its signed site.json. */
  site_title?: string
  /** The site's title for the page this permission is for. */
  page_title?: string
  /** What the site says each scope lets an agent do, in a phrase. */
  scope_labels?: readonly { scope: string; label: string }[]
}

/** The site as a person reads it: the name it publishes, else its registered name as words. */
export function siteName(grant: GrantWords): string {
  return (grant.site_title ?? '').trim() || siteLabel(grant.site)
}

/** One phrase per scope the site describes, in the grant's order; none when it describes none. */
export function scopePhrases(grant: GrantWords): string[] {
  return (grant.scope_labels ?? []).map((l) => l.label).filter(Boolean)
}
