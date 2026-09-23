import type { ReactNode } from 'react'

// The two block shapes an agent page is built from, shared by Overview and
// Settings so the two cannot drift into looking like different products.

/** One titled block. The title is a real heading — several of these used to be
 *  their own rail entries, so each still has to read as a destination — and
 *  `scroll-mt` keeps an anchor jump from tucking it under the header. */
export function Section({
  id,
  title,
  description,
  children,
}: {
  id: string
  title: string
  description: string
  children: ReactNode
}) {
  return (
    <section
      id={id}
      aria-labelledby={`${id}-title`}
      className="mb-10 scroll-mt-6 border-t border-border pt-6 first-of-type:border-t-0 first-of-type:pt-0"
    >
      <h2 id={`${id}-title`} className="text-[15px] font-semibold text-foreground">
        {title}
      </h2>
      <p className="mt-0.5 mb-4 text-[12px] text-muted-foreground">{description}</p>
      {children}
    </section>
  )
}

/** One setting: what it is, WHO may change it, and the control. "Who" is shown
 *  because several of these refuse most people, and discovering that by clicking
 *  and reading an error is the worst way to learn it. */
export function Setting({
  title,
  who,
  description,
  children,
}: {
  title: string
  who: string
  description: string
  children: ReactNode
}) {
  return (
    <div className="p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <h3 className="text-[13px] font-semibold text-foreground">{title}</h3>
        <span className="text-[11px] text-muted-foreground">{who}</span>
      </div>
      <p className="mt-1 mb-3 text-[12px] text-muted-foreground">{description}</p>
      {children}
    </div>
  )
}
