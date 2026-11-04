import { errorText } from '@/api/problem'
import { useState, type FormEvent, type JSX } from 'react'
import { Button, StatusBadge } from 'canopy-ui/ui'
import { postUpdate, publishUpdate, setUpdateSettings, type Health, type ProjectDetail, type ProjectUpdate } from '@/api/work'
import { Markdown } from '@/components/Markdown'
import { Section } from '@/components/objectPage/settingsLayout'
import { HEALTHS, displayAgent, displayPerson, healthMeta } from '@/components/work/meta'
import { relativeAge } from '@/lib/relativeAge'

function authorOf(u: ProjectUpdate): string {
  if (u.author_agent) return displayAgent(u.author_agent)
  if (u.author_email) return displayPerson(u.author_email)
  return 'someone'
}

/** ", published by …" for an agent's update — the person, or the lead agent itself. */
function publishedBy(u: ProjectUpdate): string {
  if (!u.author_agent) return ''
  if (u.published_by_agent) return `, published by ${displayAgent(u.published_by_agent)}`
  if (u.published_by_email) return `, published by ${displayPerson(u.published_by_email)}`
  return ''
}

/** "3 tasks done · 2 added · target Nov 1 → Nov 15 · Beta 4/6" from an update's `diff`. */
export function diffSummary(diff: Record<string, unknown> | undefined | null): string[] {
  if (!diff) return []
  const out: string[] = []
  const done = Number(diff.tasks_done ?? 0)
  const added = Number(diff.tasks_added ?? 0)
  if (done) out.push(`${done} task${done === 1 ? '' : 's'} done`)
  if (added) out.push(`${added} added`)
  const target = diff.target_date
  if (Array.isArray(target) && target.length === 2) out.push(`target ${target[0] ?? 'none'} → ${target[1] ?? 'none'}`)
  const ms = Array.isArray(diff.milestones) ? (diff.milestones as { name?: string; done?: number; total?: number }[]) : []
  for (const m of ms) if (m?.name) out.push(`${m.name} ${m.done ?? 0}/${m.total ?? 0}`)
  return out
}

function HealthChip({ health }: { health: string }): JSX.Element | null {
  const h = healthMeta(health)
  return h ? (
    <StatusBadge tone={h.tone} dot>
      {h.label}
    </StatusBadge>
  ) : null
}

export function UpdatesSection({
  detail,
  workspace,
  handle,
  onChanged,
}: {
  detail: ProjectDetail
  workspace: string
  handle: string
  onChanged: () => void
}): JSX.Element {
  const latest = detail.latest_update ?? null
  const drafts = detail.draft_updates ?? []
  const diff = diffSummary(latest?.diff)
  return (
    <Section id="project-update" title="Latest update" description="How it is going, in a line and a health: on track, at risk or off track.">
      {detail.update_missing && (
        <p className="mb-3 rounded border border-warning/30 bg-warning/10 px-3 py-2 text-[12px] text-warning" data-testid="update-missing">
          Update missing — this project has had no update for longer than the workspace expects.
          {detail.may_edit ? ' Post one below.' : ''}
        </p>
      )}
      {latest ? (
        <article className="rounded-lg border border-border bg-card px-4 py-3" data-testid="latest-update">
          <div className="mb-2 flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
            <HealthChip health={latest.health} />
            <span>
              {authorOf(latest)}
              {publishedBy(latest)}
            </span>
            <span title={latest.published_at ?? latest.created_at}>
              {relativeAge(latest.published_at ?? latest.created_at, new Date(), { seconds: false })}
            </span>
          </div>
          {latest.body ? <Markdown className="text-[13px]">{latest.body}</Markdown> : <p className="text-[13px] text-muted-foreground">No words, just the health.</p>}
          {diff.length > 0 && (
            <p className="mt-2 text-[12px] text-muted-foreground" data-testid="update-diff">
              Since the last update: {diff.join(' · ')}
            </p>
          )}
        </article>
      ) : (
        <p className="text-[13px] text-muted-foreground">No update yet.</p>
      )}

      {drafts.length > 0 && (
        <div className="mt-4" data-testid="draft-updates">
          <h3 className="mb-1 text-[12px] font-medium text-muted-foreground">Drafts waiting to be published ({drafts.length})</h3>
          <ul className="divide-y divide-border rounded-lg border border-border bg-card">
            {drafts.map((d) => (
              <DraftRow key={d.id} draft={d} canPublish={detail.may_edit} workspace={workspace} handle={handle} onChanged={onChanged} />
            ))}
          </ul>
        </div>
      )}

      {detail.may_edit && <PostUpdate workspace={workspace} handle={handle} onPosted={onChanged} />}
      <LeadAgentPublishes detail={detail} workspace={workspace} handle={handle} onChanged={onChanged} />
    </Section>
  )
}

/** Whether the lead agent publishes its own updates: an admin's switch, everyone else sees the state. */
function LeadAgentPublishes({
  detail,
  workspace,
  handle,
  onChanged,
}: {
  detail: ProjectDetail
  workspace: string
  handle: string
  onChanged: () => void
}): JSX.Element | null {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const lead = detail.agent_slug
  if (!lead) return null
  const on = Boolean(detail.lead_agent_may_publish)
  const name = displayAgent(lead)
  if (!detail.may_decide) {
    return on ? (
      <p className="mt-3 text-[12px] text-muted-foreground" data-testid="lead-agent-publishes">
        {name} publishes its own updates on this project.
      </p>
    ) : null
  }
  return (
    <div className="mt-3 text-[12px]" data-testid="lead-agent-publishes">
      <label className="inline-flex items-center gap-1.5 text-foreground-secondary">
        <input
          type="checkbox"
          checked={on}
          disabled={busy}
          className="accent-primary"
          onChange={(e) => {
            setBusy(true)
            setError(null)
            setUpdateSettings(workspace, handle, { lead_agent_may_publish: e.target.checked })
              .then(onChanged)
              .catch((err: unknown) => setError(errorText(err, 'Could not change it')))
              .finally(() => setBusy(false))
          }}
        />
        Let {name} publish its own updates (otherwise a person publishes its drafts)
      </label>
      {error && <p className="mt-1 text-destructive">{error}</p>}
    </div>
  )
}

function HealthRadios({ value, onChange, name }: { value: Health; onChange: (h: Health) => void; name: string }): JSX.Element {
  return (
    <span role="radiogroup" aria-label="Health" className="inline-flex flex-wrap gap-3 text-[12px]">
      {HEALTHS.map((h) => (
        <label key={h.value} className="inline-flex items-center gap-1 text-foreground-secondary">
          <input type="radio" name={name} value={h.value} checked={value === h.value} onChange={() => onChange(h.value)} className="accent-primary" />
          {h.label}
        </label>
      ))}
    </span>
  )
}

function PostUpdate({ workspace, handle, onPosted }: { workspace: string; handle: string; onPosted: () => void }): JSX.Element {
  const [health, setHealth] = useState<Health>('on_track')
  const [body, setBody] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    postUpdate(workspace, handle, { health, body: body.trim() })
      .then(() => {
        setBody('')
        onPosted()
      })
      .catch((err: unknown) => setError(errorText(err, 'Could not post the update')))
      .finally(() => setBusy(false))
  }
  return (
    <form onSubmit={submit} className="mt-4 flex flex-col gap-2" data-testid="post-update">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-[12px] font-medium text-foreground">Post an update</span>
        <HealthRadios value={health} onChange={setHealth} name="post-update-health" />
      </div>
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        aria-label="Update"
        rows={3}
        placeholder="What moved, what is next, what is in the way (Markdown)"
        className="rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder"
      />
      {error && <p className="text-[12px] text-destructive">{error}</p>}
      <div>
        <Button type="submit" size="sm" disabled={busy}>
          Post update
        </Button>
      </div>
    </form>
  )
}

function DraftRow({
  draft,
  canPublish,
  workspace,
  handle,
  onChanged,
}: {
  draft: ProjectUpdate
  canPublish: boolean
  workspace: string
  handle: string
  onChanged: () => void
}): JSX.Element {
  // The agent's suggested health, which the publisher may change.
  const [health, setHealth] = useState<Health>(HEALTHS.find((h) => h.value === draft.health)?.value ?? 'on_track')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  return (
    <li className="px-3 py-2 text-[13px]" data-testid={`draft-${draft.id}`}>
      <div className="mb-1 flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
        <span>Draft by {authorOf(draft)}</span>
        <span>{relativeAge(draft.created_at, new Date(), { seconds: false })}</span>
      </div>
      {draft.body && <Markdown className="text-[13px]">{draft.body}</Markdown>}
      {canPublish && (
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <HealthRadios value={health} onChange={setHealth} name={`draft-${draft.id}-health`} />
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => {
              setBusy(true)
              setError(null)
              publishUpdate(workspace, handle, draft.id, { health })
                .then(onChanged)
                .catch((err: unknown) => setError(errorText(err, 'Could not publish')))
                .finally(() => setBusy(false))
            }}
          >
            Publish
          </Button>
        </div>
      )}
      {error && <p className="mt-1 text-[12px] text-destructive">{error}</p>}
    </li>
  )
}
