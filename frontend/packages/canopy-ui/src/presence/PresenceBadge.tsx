import { useEffect, useRef, useState } from 'react'
import { avatarFor } from './avatar'
import type { Viewer } from './usePresence'

const MAX_AVATARS = 3

/**
 * Collapsed viewer cluster that expands into a named list.
 *
 * Renders nothing when you are alone — a badge that permanently reads "1"
 * is noise, and the account menu already tells you who you are.
 */
export function PresenceBadge({ viewers }: { viewers: Viewer[] }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (viewers.length < 2) return null

  // You first, then everyone else in roster order.
  const ordered = [...viewers].sort((a, b) => Number(b.self) - Number(a.self))
  const shown = ordered.slice(0, MAX_AVATARS)
  const overflow = ordered.length - shown.length

  return (
    <div className="relative" ref={rootRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={`${viewers.length} people viewing this page`}
        className="flex items-center -space-x-2 rounded-full p-0.5 hover:opacity-90"
      >
        {shown.map((v) => {
          const { initials, colorClass } = avatarFor(v.email, v.name)
          return (
            <span
              key={v.email}
              className={`inline-flex h-6 w-6 items-center justify-center rounded-full
                ring-2 ring-card text-[10px] font-semibold text-white ${colorClass}
                ${v.idle ? 'opacity-45' : ''}`}
            >
              {initials}
            </span>
          )
        })}
        {overflow > 0 && (
          <span
            className="inline-flex h-6 w-6 items-center justify-center rounded-full
              bg-muted ring-2 ring-card text-[10px] font-semibold text-muted-foreground"
          >
            +{overflow}
          </span>
        )}
      </button>

      {open && (
        <div
          className="absolute right-0 z-50 mt-2 w-64 rounded-md border border-border
            bg-card p-1 shadow-md"
        >
          {ordered.map((v) => {
            const { initials, colorClass } = avatarFor(v.email, v.name)
            return (
              <div key={v.email} className="flex items-center gap-2 rounded px-2 py-1.5">
                <span
                  className={`inline-flex h-6 w-6 shrink-0 items-center justify-center
                    rounded-full text-[10px] font-semibold text-white ${colorClass}
                    ${v.idle ? 'opacity-45' : ''}`}
                >
                  {initials}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm text-foreground">
                    {v.name || v.email}
                    {v.self && <span className="ml-1 text-muted-foreground">(you)</span>}
                  </span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {v.subLocation}
                  </span>
                </span>
                {v.idle && <span className="text-[10px] text-muted-foreground">idle</span>}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
