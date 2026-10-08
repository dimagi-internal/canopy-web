import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import type { JSX } from 'react'

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
  [&_table]:my-2 [&_table]:block [&_table]:overflow-x-auto [&_table]:text-[0.9em]
  [&_th]:border [&_th]:border-border [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_th]:font-semibold
  [&_td]:border [&_td]:border-border [&_td]:px-2 [&_td]:py-1
  [&_img]:my-2 [&_img]:block [&_img]:h-auto [&_img]:max-w-full [&_img]:rounded [&_img]:border [&_img]:border-border
`

// An agent can show a picture with `![caption](https://…)` (e.g. a Labs coach
// chart, a 1080px-wide PNG). Without the [&_img] rules above it overflows a
// narrow surface like the Connect Labs embed panel. Each image links to the
// full-size original in a new tab, since the inline copy is scaled down.
// urlTransform is left at react-markdown's default, so a `data:` src is still
// stripped to an empty string — no image src is rendered from it.
const components: Components = {
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

export function Markdown({
  children,
  className = '',
}: {
  children: string
  className?: string
}): JSX.Element {
  return (
    <div className={`${PROSE} ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={components}>{children}</ReactMarkdown>
    </div>
  )
}
