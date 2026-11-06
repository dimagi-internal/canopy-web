/** A registered site's name as a person reads it: `connect-labs` -> `Connect Labs`.
 *
 *  A site is known to canopy by its registered name (a slug), and the permission prompts
 *  used to print it raw. Until a site publishes a display name of its own, this is the one
 *  place that turns the slug into words, so every prompt reads the same. */
export function siteLabel(site: string): string {
  const words = (site || '').split(/[-_\s]+/).filter(Boolean)
  if (!words.length) return 'this site'
  return words.map((w) => w[0].toUpperCase() + w.slice(1)).join(' ')
}
