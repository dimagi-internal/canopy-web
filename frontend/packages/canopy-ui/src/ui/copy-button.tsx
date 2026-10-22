import type { ComponentProps, ReactNode } from 'react'

import { cn } from '../lib/cn'
import { useCopy } from '../lib/copy'

const VARIANT = {
  /** Inline text, brand-coloured — beside a value in a sentence or table. */
  link: 'text-primary hover:underline',
  /** A small bordered chip. */
  outline: 'rounded border border-border px-2 py-0.5 text-[11px] text-foreground hover:bg-muted',
  /** Borderless, muted, tiny — tucked beside a value in a dense list. */
  ghost: 'rounded px-1 text-[10px] text-muted-foreground hover:bg-muted',
} as const

/** A button that copies `text` and says so for a moment ("Copied"). Where the
 *  clipboard is refused it stays as it was and `onCopyFailed` is called — keep
 *  the value on screen so it can be selected by hand. */
function CopyButton({
  text,
  children = 'Copy',
  copiedLabel = 'Copied',
  variant = 'link',
  resetMs = 1500,
  onCopied,
  onCopyFailed,
  className,
  type = 'button',
  ...props
}: Omit<ComponentProps<'button'>, 'onClick' | 'children'> & {
  text: string
  children?: ReactNode
  copiedLabel?: ReactNode
  variant?: keyof typeof VARIANT
  resetMs?: number
  onCopied?: () => void
  onCopyFailed?: () => void
}) {
  const { copied, copy } = useCopy(resetMs)
  return (
    <button
      type={type}
      data-slot="copy-button"
      className={cn(VARIANT[variant], className)}
      onClick={() => {
        void copy(text).then((ok) => (ok ? onCopied?.() : onCopyFailed?.()))
      }}
      {...props}
    >
      {copied ? copiedLabel : children}
    </button>
  )
}

export { CopyButton }
