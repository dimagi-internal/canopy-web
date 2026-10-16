import { useEffect, useState } from 'react'
import type { Capability, Decision, Note, DecisionEdit, LeaveFeedbackIn, Storyboard } from '@/api/storyboards'
import { editDecision, leaveFeedback } from '@/api/storyboards'
import { NotesReturned } from './NotesReturned'
import { NoteComposer, readReviewerName } from './NoteComposer'
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

export function DecisionRow({ d, canEdit, needsName, onEdit, scopeTag, children }: {
  d: Decision
  canEdit: boolean
  /** A guest staging an edit must be named first; show a hint, not the buttons. */
  needsName?: boolean
  onEdit: (body: DecisionEdit) => Promise<void>
  /** The narrative this row is about, when the surrounding group does not already say. */
  scopeTag?: string
  /** Rendered under the row — the per-row note composer. */
  children?: React.ReactNode
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
      {(d.pending ?? []).map((e) => (
        <p key={e.edit_id} className="mt-1 break-words text-sm text-muted-foreground">
          Pending: {e.by_name} → {e.action === 'confirm' ? d.recommended : e.value}
        </p>
      ))}
      {editable && needsName && (
        <p className="mt-3 text-sm text-muted-foreground">Add your name in a note first</p>
      )}
      {editable && !needsName && (
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
      {children}
    </li>
  )
}

export function DecisionLog({ board, token, ws, canEdit, inRound, capability, notesByAnchor }: {
  board: Storyboard
  token?: string | null
  ws?: string | null
  canEdit: boolean
  /** A feedback round is open: guests with the edit grant may stage edits. */
  inRound?: boolean
  capability?: Capability
  /** Round notes anchored to a row (`decision:<id>`), shown under it. */
  notesByAnchor?: Map<string, Note[]>
}) {
  // Local copy: an edit returns the updated row, which replaces its old self here.
  const [rows, setRows] = useState<Decision[]>(board.decisions ?? [])
  // A reload (round opened, closed, edit staged) hands us fresh rows.
  useEffect(() => setRows(board.decisions ?? []), [board.decisions])
  const [name, setName] = useState(() => readReviewerName())
  const onChanged = (d: Decision) => setRows((rs) => rs.map((x) => (x.id === d.id ? d : x)))
  const titles = narrativeTitles(board)
  const groups = groupDecisions(rows, titles)
  if (groups.length === 0) return null
  const cap = capability ?? board.capability
  const round = inRound ?? !!board.round
  const isGuest = !canEdit
  const guestMayStage = isGuest && round && cap === 'suggest'
  const rowFor = (d: Decision, scopeTag?: string) => (
    <DecisionRow key={d.id} d={d} canEdit={(canEdit || guestMayStage) && !d.superseded_by} scopeTag={scopeTag}
        needsName={isGuest && !name.trim()}
        onEdit={async (body) =>
          onChanged(
            await editDecision(
              board.slug, d.id, isGuest ? { ...body, author_name: name.trim() } : body, token, ws,
            ),
          )}>
      <NotesReturned notes={notesByAnchor?.get(`decision:${d.id}`) ?? []} />
      {(cap === 'comment' || cap === 'suggest') && (
        <NoteComposer
          capability={cap}
          anchorLabel={d.plain_question || d.question}
          cta="Leave a note on this question"
          defaults={{ anchor_id: `decision:${d.id}` }}
          onSubmit={async (payload: LeaveFeedbackIn) => {
            await leaveFeedback(board.slug, payload, token, ws)
            setName(readReviewerName())
          }}
        />
      )}
    </DecisionRow>
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
