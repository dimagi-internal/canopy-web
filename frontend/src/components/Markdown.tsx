import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import { Children, isValidElement, type JSX, type ReactNode } from 'react'
import { CopyButton } from 'canopy-ui/ui'

// Shared markdown renderer for agent-authored text (Item bodies, turn/sync
// summaries, and any other surface that shows model output). One place so the
// prose styling — semantic tokens only, so it works in both light and dark —
// never drifts across the app. (react-markdown is already in the eager bundle
// via SystemPage/ShareoutsPage, so this imports it directly rather than
// code-splitting.)
//
// remark-breaks turns single newlines into <br>, so model output authored with
// bare line breaks (rather than blank-line paragraphs) keeps its intended shape
// — the behavior the turn/sync summaries previously got from whitespace-pre-wrap.

// Token-based prose styling. The caller's `className` sets the base text size and
// color (e.g. `text-[12px] text-foreground-secondary`); block children inherit it.
// First/last margins are collapsed so a card body sits flush.
const PROSE = `
  [&>*:first-child]:mt-0 [&>*:last-child]:mb-0
  [&_p]:my-1.5
  [&_ul]:my-1.5 [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:space-y-0.5
  [&_ol]:my-1.5 [&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:space-y-0.5
  [&_li]:pl-0.5
  [&_h1]:text-[1.05em] [&_h1]:font-semibold [&_h1]:text-foreground [&_h1]:mt-2 [&_h1]:mb-1
  [&_h2]:font-semibold [&_h2]:text-foreground [&_h2]:mt-2 [&_h2]:mb-1
  [&_h3]:font-semibold [&_h3]:text-foreground [&_h3]:mt-2 [&_h3]:mb-1
  [&_strong]:font-semibold [&_strong]:text-foreground
  [&_em]:italic
  [&_a]:text-primary [&_a]:underline [&_a]:underline-offset-2
  [&_code]:rounded [&_code]:bg-background [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-primary [&_code]:text-[0.85em]
  [&_pre]:my-2 [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:border [&_pre]:border-border [&_pre]:bg-background [&_pre]:p-2 [&_pre]:text-[0.85em]
  [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre_code]:text-inherit
  [&_blockquote]:my-1.5 [&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-3 [&_blockquote]:text-muted-foreground
  [&_hr]:my-3 [&_hr]:border-border
  [&_table]:w-max [&_table]:min-w-full [&_table]:border-separate [&_table]:border-spacing-0 [&_table]:text-[0.9em]
  [&_th]:sticky [&_th]:top-0 [&_th]:z-[1] [&_th]:bg-muted [&_th]:border-b [&_th]:border-r [&_th]:border-border [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_th]:font-semibold [&_th]:whitespace-nowrap
  [&_td]:border-b [&_td]:border-r [&_td]:border-border [&_td]:px-2 [&_td]:py-1 [&_td]:align-top
  [&_img]:my-2 [&_img]:block [&_img]:h-auto [&_img]:max-w-full [&_img]:rounded [&_img]:border [&_img]:border-border
`

// An agent can show a picture with `![caption](https://…)` (e.g. a Labs coach
// chart, a 1080px-wide PNG). Without the [&_img] rules above it overflows a
// narrow surface like the Connect Labs embed panel. Each image links to the
// full-size original in a new tab, since the inline copy is scaled down.
// urlTransform is left at react-markdown's default, so a `data:` src is still
// stripped to an empty string — no image src is rendered from it.
/** The plain text of a rendered node — what a code block's Copy copies. */
function textOf(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children)
  return ''
}

const components: Components = {
  // Data answers are tables: a wide one scrolls sideways inside its own box
  // (never the panel), and a long one scrolls down with its header pinned.
  table: ({ children }) => (
    <div data-slot="md-table" className="my-2 max-h-[70vh] max-w-full overflow-auto rounded border-l border-t border-border">
      <table>{children}</table>
    </div>
  ),
  // A query or a JSON payload is something you paste somewhere else.
  pre: ({ children }) => {
    const text = textOf(Children.toArray(children)).replace(/\n$/, '')
    return (
      <div data-slot="md-code" className="group relative">
        <pre>{children}</pre>
        <CopyButton
          text={text}
          variant="outline"
          aria-label="Copy code"
          className="absolute right-1.5 top-1.5 bg-card opacity-70 hover:opacity-100 focus-visible:opacity-100"
        />
      </div>
    )
  },
  img: ({ src, alt, title }) => {
    const img = (
      <img src={src} alt={alt ?? ''} title={title} loading="lazy" referrerPolicy="no-referrer" />
    )
    if (typeof src !== 'string' || !src) return img
    return (
      <a href={src} target="_blank" rel="noopener noreferrer">
        {img}
      </a>
    )
  },
}

// Inside an embedded panel a link must leave the panel. Followed in place it
// loads the target INTO the panel's iframe, and a site that refuses framing
// (Labs does) shows "refused to connect" where the answer was (2026-10-13: the
// PMC explorer link an agent gave on Labs' targeting page). A new tab is a
// normal page: the frame's sandbox lets popups escape it (#1359).
const newTabComponents: Components = {
  ...components,
  a: ({ href, children, title }) => (
    <a href={href} title={title} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
}

export function Markdown({
  children,
  className = '',
  linksInNewTab = false,
}: {
  children: string
  className?: string
  /** Open every link in a new tab: for surfaces in someone else's page (the embed). */
  linksInNewTab?: boolean
}): JSX.Element {
  return (
    <div className={`${PROSE} ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={linksInNewTab ? newTabComponents : components}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
