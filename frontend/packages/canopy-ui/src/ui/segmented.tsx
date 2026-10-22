import type { ReactNode } from 'react'

import { cn } from '../lib/cn'

export interface SegmentedOption<T extends string> {
  value: T
  label: ReactNode
}

const SIZE = {
  xs: 'px-2 py-0.5 text-[11px]',
  sm: 'px-2.5 py-1 text-[12px]',
  md: 'px-3 py-1.5 text-[13px]',
} as const

/** A one-of-N switch: a radiogroup of buttons, the chosen one filled with the
 *  brand. `label` names the group for a screen reader. Picking the current
 *  value again does nothing. */
function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
  size = 'sm',
  className,
}: {
  value: T
  options: readonly SegmentedOption<T>[]
  onChange: (value: T) => void
  label: string
  size?: keyof typeof SIZE
  className?: string
}) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      data-slot="segmented"
      className={cn('inline-flex rounded-md border border-border bg-card p-0.5', className)}
    >
      {options.map((o) => {
        const on = o.value === value
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => {
              if (!on) onChange(o.value)
            }}
            className={cn(
              'rounded font-medium transition-colors',
              SIZE[size],
              on ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}

export { Segmented }
