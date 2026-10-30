// The pieces of the new-agent form: a numbered step, a field, the repo's state, a creation problem, and the remembered owner. The page and its state are `NewAgentPage`.
import { type JSX, type ReactNode } from 'react'
import { CheckCircle2 } from 'lucide-react'
import { Button, } from 'canopy-ui/ui'
import {
  CreationError,
  type RepoCheckOut,
} from '@/api/agentCreation'

export function RepoState({
  check,
  error,
  checking,
  onUseSlug,
}: {
  check: RepoCheckOut | null
  error: CreationError | null
  checking: boolean
  /** For a repo that already holds another agent: take that agent's slug. */
  onUseSlug: (slug: string) => void
}): JSX.Element | null {
  if (error) return <Problem error={error} />
  if (checking) return <p className="text-[12px] text-muted-foreground">Checking…</p>
  if (!check || check.state === 'no_token') return null
  if (check.state === 'ready' || check.state === 'update' || check.state === 'adopt') {
    return (
      <p className="flex items-center gap-1.5 text-[13px] text-success">
        <CheckCircle2 className="h-4 w-4 shrink-0" aria-hidden="true" />
        {check.state === 'ready' ? `${check.repo} is empty; pushing as @${check.login}.` : check.detail}
      </p>
    )
  }
  if (check.state === 'name_mismatch' && check.suggested_slug) {
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-[13px] text-foreground">
        <span>{check.detail}</span>
        <Button size="sm" variant="outline" onClick={() => onUseSlug(check.suggested_slug)}>
          Use “{check.suggested_slug}”
        </Button>
      </div>
    )
  }
  return (
    <p className="rounded-md border border-warning/40 bg-warning/10 p-3 text-[13px] text-foreground">
      {check.detail} This checks again when you come back to the tab.
    </p>
  )
}

export function Problem({ error }: { error: CreationError }): JSX.Element {
  return <p className="mt-3 text-[13px] text-destructive">{error.message}</p>
}

/** The owner last typed, per browser — a convenience only, so a blocked storage
 *  read is just an empty field. */
export const OWNER_KEY = 'canopy:new-agent-owner'




export function rememberedOwner(): string {
  try {
    return window.localStorage.getItem(OWNER_KEY) ?? ''
  } catch {
    return ''
  }
}

export function remember(owner: string): void {
  try {
    window.localStorage.setItem(OWNER_KEY, owner)
  } catch {
    // storage blocked; nothing to keep
  }
}

export function Step({ n, title, done, children }: { n: number; title: string; done: boolean; children: ReactNode }) {
  return (
    <section className="mb-6 rounded-lg border border-border bg-card p-4" aria-labelledby={`step-${n}`}>
      <h2 id={`step-${n}`} className="mb-3 flex items-center gap-2 text-[14px] font-semibold text-foreground">
        {done ? (
          <CheckCircle2 className="h-4 w-4 text-success" aria-label="done" />
        ) : (
          <span className="flex h-4 w-4 items-center justify-center rounded-full border border-border text-[10px] text-muted-foreground">
            {n}
          </span>
        )}
        {title}
      </h2>
      <div className="flex flex-col gap-3">{children}</div>
    </section>
  )
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[12px] font-medium text-foreground">{label}</span>
      {children}
      {hint && <span className="text-[11px] text-muted-foreground">{hint}</span>}
    </label>
  )
}
