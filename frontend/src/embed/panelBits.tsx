import { useEffect, useState } from 'react'
import { Markdown } from '@/components/Markdown'
import type { HostLink } from './hostLink'

/** The same renderer canopy's own chat page injects, for the same reason.
 *
 *  `ChatPanel` defaults to `plainText` when this seam is left unwired, which is
 *  what the widget did — so an agent's reply arrived with its markdown as
 *  literal source: `**bold**`, `- ` before every bullet, unrendered fences
 *  around code. Same agent, same words, visibly worse than canopy-web, which
 *  is precisely the complaint. */
export function renderMarkdown(text: string) {
  return (
    <Markdown className="text-sm leading-relaxed" linksInNewTab>
      {text}
    </Markdown>
  )
}

/** What the host page last declared it is showing, kept current. */
export function usePageState(link: HostLink): Record<string, unknown> | null {
  const [state, setState] = useState(() => link.pageState())
  useEffect(() => link.onPageStateChanged(setState), [link])
  return state
}

/** One line under the header saying which page the agent is told about — the
 *  page the host declares, which canopy hands the agent with every turn. Nothing
 *  when the host declares none, since then the agent is told nothing either. */
export function AskingAbout({ pageName }: { pageName: string }) {
  if (!pageName) return null
  return (
    <p className="truncate border-b border-border px-3 py-1 text-[11px] text-muted-foreground" data-testid="asking-about">
      Asking about <span className="text-foreground">{pageName}</span>
    </p>
  )
}

export function Centered({ children }: { children: React.ReactNode }) {
  return <div className="grid h-full place-items-center p-4 text-muted-foreground">{children}</div>
}
