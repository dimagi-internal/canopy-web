import type { ComponentProps } from 'react'

import { cn } from '../lib/cn'

/** A square button holding only an icon. `label` is required: it is the
 *  accessible name and the hover title, since there is no visible text.
 *  `danger` tints the hover destructive (forget, remove). */
function IconButton({
  label,
  danger = false,
  size = 'sm',
  className,
  type = 'button',
  ...props
}: Omit<ComponentProps<'button'>, 'aria-label'> & {
  label: string
  danger?: boolean
  size?: 'sm' | 'md'
}) {
  return (
    <button
      type={type}
      aria-label={label}
      title={label}
      data-slot="icon-button"
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded text-muted-foreground transition-colors disabled:pointer-events-none disabled:opacity-40',
        size === 'sm' ? 'p-1' : 'h-8 w-8',
        danger ? 'hover:bg-destructive/10 hover:text-destructive' : 'hover:bg-muted hover:text-foreground',
        className,
      )}
      {...props}
    />
  )
}

export { IconButton }
