import { useEffect, useState, type ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'
import { useLocation } from 'react-router-dom'

// One collapsible block of a vault's page. Closed it is a single line: the title
// and a summary of what is inside ("7 secrets · 1 needs a value"), so a long page
// reads as an outline. The body stays mounted while closed (just hidden), so its
// data loads, its anchors exist and a link into it (`#cred-zk`) can open it.
//
// It opens by itself when something inside needs the reader (`tone="attention"`,
// until they close it) and when the address points at it: its own id or any of
// `anchors`, which is how the old `settings#…` links keep landing somewhere visible.

export type VaultTone = 'ok' | 'attention' | 'quiet'

export function VaultSection({
  id,
  title,
  summary,
  tone = 'quiet',
  anchors = [],
  defaultOpen = false,
  children,
}: {
  id: string
  title: string
  /** One line of what is inside, shown beside the title while closed AND open. */
  summary?: ReactNode
  tone?: VaultTone
  /** Other addresses (`#cred-zk`) that mean "this section". */
  anchors?: string[]
  defaultOpen?: boolean
  children: ReactNode
}) {
  const { hash } = useLocation()
  const target = decodeURIComponent(hash.replace(/^#/, ''))
  const pointedAt = target !== '' && (target === id || anchors.includes(target))
  const [open, setOpen] = useState(defaultOpen || tone === 'attention' || pointedAt)
  const [chose, setChose] = useState(false)

  useEffect(() => {
    if (pointedAt) setOpen(true)
  }, [pointedAt, hash])
  // Needing the reader opens it, unless they already shut it.
  useEffect(() => {
    if (tone === 'attention' && !chose) setOpen(true)
  }, [tone, chose])

  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-6 border-t border-border first-of-type:border-t-0">
      <h3 id={`${id}-title`} className="m-0">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={`${id}-body`}
          data-testid={`vault-section-${id}`}
          onClick={() => {
            setChose(true)
            setOpen((o) => !o)
          }}
          className="flex min-h-11 w-full items-center gap-2 py-2 text-left sm:min-h-0 sm:py-3"
        >
          <ChevronRight aria-hidden="true" className={`h-4 w-4 shrink-0 text-muted-foreground transition-transform ${open ? 'rotate-90' : ''}`} />
          <span className="text-[13px] font-semibold text-foreground">{title}</span>
          {summary && (
            <span
              className={`text-[12px] font-normal ${tone === 'attention' ? 'text-warning' : 'text-muted-foreground'}`}
              data-testid={`vault-section-${id}-summary`}
            >
              {summary}
            </span>
          )}
        </button>
      </h3>
      <div id={`${id}-body`} hidden={!open} className="pb-4 pl-6">
        {children}
      </div>
    </section>
  )
}
