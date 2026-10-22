import { useState } from 'react'
import type { StagedEdit, Storyboard } from '@/api/storyboards'
import { closeRound, openRound } from '@/api/storyboards'

const norm = (s: string) => s.trim().replace(/\s+/g, ' ').toLowerCase()
// Mirrors the server's conflict rule: a write-in is a pick, picking the
// recommended option is the same intent as confirming it, and values compare
// without case or spacing. `recommended` maps a row id to its recommended value.
const intent = (e: StagedEdit, recommended: Record<string, string>) => {
  const value = norm(e.value)
  if (e.action === 'confirm' || (e.action === 'pick' || e.action === 'write_in') && value === norm(recommended[e.key] ?? '\u0000')) {
    return 'confirm'
  }
  return `${e.action === 'write_in' ? 'pick' : e.action}|${value}`
}

/** Rows where people staged different edits — the closer must pick one each. */
export function conflictsOf(
  staged: readonly StagedEdit[],
  recommended: Record<string, string> = {},
): Record<string, StagedEdit[]> {
  const byKey: Record<string, StagedEdit[]> = {}
  for (const e of staged) (byKey[e.key] ??= []).push(e)
  return Object.fromEntries(
    Object.entries(byKey).filter(([, es]) => new Set(es.map((e) => intent(e, recommended))).size > 1),
  )
}

const primary = 'rounded-md bg-primary px-3 py-1 text-primary-foreground disabled:opacity-50'

export function RoundBanner({ board, canManage, ws, onChanged }: {
  board: Storyboard
  canManage: boolean
  ws?: string | null
  onChanged: () => void
}) {
  const round = board.round
  const [closing, setClosing] = useState(false)
  const [note, setNote] = useState('')
  const [picks, setPicks] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const run = async (fn: () => Promise<unknown>) => {
    setError('')
    setBusy(true)
    try {
      await fn()
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'That failed')
    } finally {
      setBusy(false)
    }
  }
  if (!round) {
    // Closed but not yet consumed: the agent has it, and opening another would 409.
    const waiting = board.previous_round?.state === 'closed' ? board.previous_round : null
    if (waiting) {
      return (
        <div className="mb-6 rounded-md border border-border bg-muted p-4 text-sm">
          <p className="text-foreground">
            Round {waiting.number} is closed and with the agent; the next round opens when it
            publishes the new version.
          </p>
        </div>
      )
    }
    if (!canManage) return null
    return (
      <div className="mb-6 rounded-md border border-border bg-muted p-4 text-sm">
        <p className="text-foreground">
          No feedback round is open. Open one to collect everyone’s notes, then close it to start the next run.
        </p>
        <button type="button" disabled={busy} className={`mt-2 ${primary}`}
          onClick={() => run(() => openRound(board.slug, ws))}>Open a feedback round</button>
        {error && <p role="alert" className="mt-2 text-destructive">{error}</p>}
      </div>
    )
  }
  const decisions = board.decisions ?? []
  // The server skips a replaced row and one removed mid-round, so neither
  // needs a pick: keep only keys whose decision exists and is not superseded.
  const live = new Set(decisions.filter((d) => !d.superseded_by).map((d) => d.id))
  const conflicts = Object.fromEntries(
    Object.entries(
      conflictsOf(
        round.staged_edits,
        Object.fromEntries(decisions.map((d) => [d.id, d.recommended])),
      ),
    ).filter(([key]) => live.has(key)),
  )
  const unpicked = Object.keys(conflicts).filter((k) => !picks[k])
  return (
    <div className="mb-6 rounded-md border border-border bg-muted p-4 text-sm">
      <p className="text-foreground">
        Feedback round {round.number} is open. Everyone’s notes are shown. {round.note_count}{' '}
        {round.note_count === 1 ? 'note' : 'notes'} from {round.people_count}{' '}
        {round.people_count === 1 ? 'person' : 'people'} so far.
      </p>
      {canManage && !closing && (
        <button type="button" className={`mt-2 ${primary}`} onClick={() => setClosing(true)}>
          Close round and start the next run
        </button>
      )}
      {canManage && closing && (
        <div className="mt-3 flex flex-col gap-3">
          {Object.keys(conflicts).length > 0 && (
            <fieldset className="flex flex-col gap-3">
              <legend className="font-medium text-foreground">People disagreed. Pick one answer for each:</legend>
              {Object.entries(conflicts).map(([key, edits]) => (
                <div key={key} className="flex flex-col gap-1">
                  <span className="text-muted-foreground">
                    {decisions.find((d) => d.id === key)?.plain_question || key}
                  </span>
                  {edits.map((e) => (
                    <label key={e.edit_id} className="flex items-start gap-2 break-words">
                      <input type="radio" name={`pick-${key}`} className="mt-1"
                        checked={picks[key] === e.edit_id}
                        onChange={() => setPicks((p) => ({ ...p, [key]: e.edit_id }))} />
                      <span className="min-w-0">
                        {e.by_name}: {e.action === 'confirm' ? 'keep the recommendation' : e.value}
                      </span>
                    </label>
                  ))}
                </div>
              ))}
            </fieldset>
          )}
          <textarea aria-label="Note for the agent" value={note} onChange={(e) => setNote(e.target.value)}
            placeholder="Anything the agent should know (optional)"
            className="w-full rounded-md border border-border bg-background p-2 text-foreground" />
          <div className="flex flex-wrap gap-2">
            <button type="button" disabled={busy || unpicked.length > 0} className={primary}
              onClick={() => run(() => closeRound(board.slug, round.number, { close_note: note, picks }, ws))}>
              Close and hand to the agent
            </button>
            <button type="button" className="rounded-md border border-border px-3 py-1 text-foreground"
              onClick={() => setClosing(false)}>Cancel</button>
          </div>
        </div>
      )}
      {error && <p role="alert" className="mt-2 text-destructive">{error}</p>}
    </div>
  )
}
