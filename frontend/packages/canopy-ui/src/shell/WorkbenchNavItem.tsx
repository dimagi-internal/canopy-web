import { cloneElement, isValidElement, type JSX, type ReactElement, type ReactNode } from 'react'
import { cn } from '../lib/cn'

export function workbenchNavItemClass({
  active,
  variant = 'accent',
}: {
  active?: boolean
  variant?: 'accent' | 'neutral'
}): string {
  const activeClass =
    variant === 'neutral'
      ? 'bg-accent border-transparent text-foreground font-medium'
      : 'bg-primary/10 border-primary/30 text-primary font-medium'
  // `min-h-11` below `sm` is the 44px touch minimum; from `sm` up the rail keeps
  // its original density, where the pointer is precise and vertical space is dear.
  return cn(
    'flex min-h-11 items-center justify-between gap-2 rounded-md border px-3 py-1.5 text-sm transition-colors sm:min-h-0',
    active
      ? activeClass
      : 'border-transparent text-muted-foreground hover:bg-accent hover:text-foreground',
  )
}

interface WorkbenchNavItemProps {
  active?: boolean
  /** Active-state accent: 'accent' = orange primary tint (default), 'neutral' = grey highlight. */
  variant?: 'accent' | 'neutral'
  count?: number
  /** When true, merge styling onto the single child element (e.g. a router Link). */
  asChild?: boolean
  children: ReactNode
}

/**
 * One rail entry: label + optional right-aligned count badge + active state.
 *
 * Default form: renders a presentational <div>; the caller wraps it in their own
 * Link/NavLink. asChild form: clones the single child, merges styling onto it,
 * and uses the child's own text as the label (router-agnostic; no radix dep).
 */
export function WorkbenchNavItem({
  active,
  variant,
  count,
  asChild,
  children,
}: WorkbenchNavItemProps): JSX.Element {
  const className = workbenchNavItemClass({ active, variant })
  const badge =
    count !== undefined ? (
      <span className="shrink-0 text-[11px] text-muted-foreground">{count}</span>
    ) : null

  // asChild requires a single valid element; a non-element child falls back to the div form.
  if (asChild && isValidElement(children)) {
    const el = children as ReactElement<{ className?: string; children?: ReactNode }>
    return cloneElement(
      el,
      { className: cn(className, el.props.className) },
      <>
        <span className="truncate">{el.props.children}</span>
        {badge}
      </>,
    )
  }

  return (
    <div className={className}>
      <span className="truncate">{children}</span>
      {badge}
    </div>
  )
}
