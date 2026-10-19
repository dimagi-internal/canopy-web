import { Markdown } from '@/components/Markdown'

/** Render assistant/system message text through canopy's shared Markdown (the
 *  same renderer used across every AI-output surface — so it picks up remark-gfm
 *  AND remark-breaks, i.e. single newlines become line breaks instead of
 *  collapsing). Injected into the kit via its `renderMarkdown` seam so the kit
 *  itself stays free of react-markdown.
 *
 *  Every link in a chat opens in a new tab, canopy's own pages included
 *  (Jonathan, 2026-10-19): followed in place, a link replaced the session you
 *  were reading — in /ide, the whole workbench. */
export function renderChatMarkdown(text: string) {
  return (
    <Markdown className="text-sm leading-relaxed" linksInNewTab>
      {text}
    </Markdown>
  )
}
