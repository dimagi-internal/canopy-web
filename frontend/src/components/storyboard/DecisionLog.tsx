import { useState } from 'react'
import type { Decision, DecisionEdit, Storyboard } from '@/api/storyboards'
import { editDecision } from '@/api/storyboards'
import { groupDecisions, narrativeTitles, scopeLabel } from './decisionGroups'

/**
 * The set's decision log — ace-web's pattern (spec B). Grouping and scope
 * labels live in decisionGroups.ts.
 */

function chip(d: Decision): string {
  if (d.status === 'overridden') return d.decided_by ? `Changed by ${d.decided_by}` : 'Changed'
  if (d.confirmed) return d.decided_by ? `Confirmed by ${d.decided_by}` : 'Confirmed'
  if (d.status === 'deferred') return d.revisit_when ? `Later: ${d.revisit_when}` : 'Not needed now'
  return 'Recommended'
}

export function DecisionRow({ d, canEdit, onEdit, scopeTag }: {
  d: Decision
  canEdit: boolean
  onEdit: (body: DecisionEdit) => Promise<void>
  /** The narrative this row is about, when the surrounding group does not already say. */
  scopeTag?: string
}) {
  const [busy, setBusy] = useState(false)
  const [writeIn, setWriteIn] = useState('')
  const [error, setError] = useState('')
  const editable = canEdit && d.editable
  const run = async (body: DecisionEdit) => {
    setBusy(true)
    setError('')
    try {
      await onEdit(body)
      setWriteIn('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'That did not save')
    } finally {
      setBusy(false)
    }
  }
  return (
    <li id={`decision-${d.id}`} className="rounded-md border border-border bg-card p-4">
      {scopeTag && (
        <p className="mb-1 text-xs text-muted-foreground">{scopeTag}</p>
      )}
      <p className="font-medium text-foreground">{d.plain_question || d.question}</p>
      {d.value && (
        <p className="mt-2 flex flex-wrap items-center gap-2 text-sm">
          <span className="text-foreground">{d.value}</span>
          <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">{chip(d)}</span>
        </p>
      )}
      {d.evidence_basis === 'conflicting' && d.conflict_signals.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-sm text-muted-foreground">
          {d.conflict_signals.map((s) => <li key={s}>{s}</li>)}
        </ul>
      )}
      {(d.reasoning || d.confirm_reason) && (
        <details className="mt-2 text-sm text-muted-foreground">
          <summary>Why</summary>
          {d.confirm_reason && <p>{d.confirm_reason}</p>}
          {d.reasoning && <p>{d.reasoning}</p>}
          {d.override_reasoning && <p>{d.decided_by}: {d.override_reasoning}</p>}
        </details>
      )}
      {editable && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
          {d.value && !d.confirmed && (
            <button type="button" disabled={busy} onClick={() => run({ action: 'confirm' })}
              className="rounded-md bg-primary px-3 py-1 text-primary-foreground">Confirm</button>
          )}
          {d.options.filter((o) => o !== d.value).map((o) => (
            <button key={o} type="button" disabled={busy} onClick={() => run({ action: 'pick', value: o })}
              className="rounded-md border border-border px-3 py-1">{o}</button>
          ))}
          <input aria-label="Write your own answer" value={writeIn} onChange={(e) => setWriteIn(e.target.value)}
            placeholder="Or write your own" className="rounded-md border border-border bg-background px-2 py-1" />
          {writeIn.trim() && (
            <button type="button" disabled={busy}
              onClick={() => run({ action: 'write_in', value: writeIn.trim() })}
              className="rounded-md border border-border px-3 py-1">Use this</button>
          )}
          {d.history.length > 0 && (
            <button type="button" disabled={busy} onClick={() => run({ action: 'undo' })}
              className="text-muted-foreground underline">Undo</button>
          )}
        </div>
      )}
      {error && <p role="alert" className="mt-2 text-sm text-destructive">{error}</p>}
    </li>
  )
}

export function DecisionLog({ board, token, ws, canEdit }: {
  board: Storyboard
  token?: string | null
  ws?: string | null
  canEdit: boolean
}) {
  // Local copy: an edit returns the updated row, which replaces its old self here.
  const [rows, setRows] = useState<Decision[]>(board.decisions ?? [])
  const onChanged = (d: Decision) => setRows((rs) => rs.map((x) => (x.id === d.id ? d : x)))
  const titles = narrativeTitles(board)
  const groups = groupDecisions(rows, titles)
  if (groups.length === 0) return null
  const rowFor = (d: Decision, scopeTag?: string) => (
    <DecisionRow key={d.id} d={d} canEdit={canEdit && !d.superseded_by} scopeTag={scopeTag}
      onEdit={async (body) => onChanged(await editDecision(board.slug, d.id, body, token, ws))} />
  )
  return (
    <section aria-label="Decisions" className="mt-14 flex flex-col gap-6">
      <h2 className="text-xl font-semibold text-foreground">Before you share this</h2>
      {groups.map((g) => (
        <details key={g.title} open={g.open}>
          <summary className="cursor-pointer font-medium text-foreground">
            {g.title} <span className="text-muted-foreground">({g.rows.length})</span>
          </summary>
          {g.byScope ? (
            g.byScope.map((s) => (
              <div key={s.scope} className="mt-3">
                <h3 className="text-sm font-medium text-muted-foreground">{s.label}</h3>
                <ul className="mt-2 flex flex-col gap-3">{s.rows.map((d) => rowFor(d))}</ul>
              </div>
            ))
          ) : (
            <ul className="mt-3 flex flex-col gap-3">
              {g.rows.map((d) => rowFor(d, d.scope ? scopeLabel(d.scope, titles) : undefined))}
            </ul>
          )}
        </details>
      ))}
    </section>
  )
}
