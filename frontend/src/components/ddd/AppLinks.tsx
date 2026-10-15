/** A live product screen a recorded cut films (its upload's reference links). */
export interface AppLink {
  label: string
  url: string
}

/** Where the link goes, said plainly: "Labs" for the Connect Labs product,
 *  else the site's own host. */
export function productName(url: string): string {
  try {
    const host = new URL(url).hostname
    return host.startsWith('labs.') ? 'Labs' : host
  } catch {
    return 'the product'
  }
}

/**
 * The product screens behind a cut, as links a reviewer can open.
 *
 * A comment on a recording can only be about the recording. Opening the live
 * page it films is how a reviewer gives feedback on the product itself, so
 * every cut names the screens it shows, even when its video is not shared.
 */
export function AppLinks({ links }: { links?: readonly AppLink[] | null }) {
  if (!links?.length) return null
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1">
      {links.map((l) => (
        <li key={l.url}>
          <a
            href={l.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary hover:underline"
            title={l.url}
          >
            {`Open ${l.label} in ${productName(l.url)}`}
            <span aria-hidden> ↗</span>
          </a>
        </li>
      ))}
    </ul>
  )
}
