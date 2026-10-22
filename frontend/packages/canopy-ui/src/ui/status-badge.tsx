import type { ComponentProps } from 'react'

import { cn } from '../lib/cn'
import { toneClasses, toneDot, type StatusTone } from '../lib/tone'

/** A status pill in one of the semantic tones — the documented tinted recipe
 *  (`bg-<t>/10 text-<t> border-<t>/30`). `dot` adds a leading dot, `pulse`
 *  animates it (something in flight). Dense: 20px tall, 11px text. */
function StatusBadge({
  tone = 'neutral',
  dot = false,
  pulse = false,
  className,
  children,
  ...props
}: ComponentProps<'span'> & { tone?: StatusTone; dot?: boolean; pulse?: boolean }) {
  return (
    <span
      data-slot="status-badge"
      data-tone={tone}
      className={cn(
        'inline-flex h-5 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2 text-[11px] font-medium',
        toneClasses(tone),
        className,
      )}
      {...props}
    >
      {(dot || pulse) && (
        <span aria-hidden="true" className={cn('size-1.5 rounded-full', toneDot(tone), pulse && 'animate-pulse')} />
      )}
      {children}
    </span>
  )
}

export { StatusBadge }
