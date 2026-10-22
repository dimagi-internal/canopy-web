import { Check, Eye, Pencil, Trash2, X } from 'lucide-react'
import { IconButton } from 'canopy-ui/ui'

import type { PersonFactDetail } from '@/api/people'
import { relativeAge } from '@/lib/relativeAge'
import { IN_WORDS, reads, type AgentAccess } from './words'

/** Where an entry came from, as a short chip: "You" or "ACE · Oct 7". */
export function sourceOf(f: PersonFactDetail): { mine: boolean; text: string } {
  const when = f.created_at ? new Date(f.created_at).toLocaleDateString([], { month: 'short', day: 'numeric' }) : ''
  const site = (f.provenance_site as { name?: string } | undefined)?.name
  if (f.learned_by) return { mine: false, text: `${f.learned_by}${site ? ` on ${site}` : ''}${when ? ` · ${when}` : ''}` }
  if (!f.workspace || f.basis === 'declared') return { mine: true, text: `You${when ? ` · ${when}` : ''}` }
  return { mine: false, text: `${f.asserted_by || 'an agent'}${when ? ` · ${when}` : ''}` }
}

export type FactEdit = { id: number; text: string }

export interface FactRowProps {
  fact: PersonFactDetail
  /** The entry being corrected, if any (only this row's matters). */
  editing: FactEdit | null
  onEdit: (edit: FactEdit | null) => void
  onSave: (fact: PersonFactDetail) => void
  onForget: (fact: PersonFactDetail) => void
  /** The fact id a write is in flight on. */
  busy: number | null
  readOnly: boolean
  /** Memory is on (correcting needs it; forgetting does not). */
  enabled: boolean
  /** agent slug → display name, for "Given to …". */
  names: Map<string, string>
  /** The agent whose view the page is looking through; out-of-reach rows fade. */
  lens: AgentAccess | null
}

/** One remembered entry: its text (or the correction box), who wrote it, and
 *  how often agents were given it. Correct / Forget show on hover. */
export function FactRow({ fact: f, editing, onEdit, onSave, onForget, busy, readOnly, enabled, names, lens }: FactRowProps) {
  const src = sourceOf(f)
  const u = f.used
  const isEditing = editing?.id === f.id
  const hidden = lens !== null && !reads(lens, f.workspace ?? null)
  return (
    <li className={`group relative py-2.5 transition-opacity ${hidden ? 'opacity-25' : ''}`}>
      {isEditing ? (
        <form
          className="flex items-start gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            onSave(f)
          }}
        >
          <textarea
            autoFocus
            aria-label="Correct this"
            value={editing.text}
            onChange={(e) => onEdit({ id: f.id, text: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === 'Escape') onEdit(null)
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                onSave(f)
              }
            }}
            rows={2}
            maxLength={500}
            className="min-w-0 flex-1 resize-none rounded-md border border-primary/50 bg-background px-2 py-1 text-sm text-foreground outline-none"
          />
          <IconButton label="Save" onClick={() => onSave(f)}>
            <Check className="h-4 w-4" />
          </IconButton>
          <IconButton label="Cancel" onClick={() => onEdit(null)}>
            <X className="h-4 w-4" />
          </IconButton>
        </form>
      ) : (
        <div className="flex items-start gap-2">
          <p className={`min-w-0 flex-1 text-sm text-foreground ${busy === f.id ? 'opacity-50' : ''}`}>{f.statement}</p>
          {!readOnly && (
            <div className="flex shrink-0 gap-0.5 opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-within:opacity-100">
              {enabled && f.entry_id && (
                <IconButton label="Correct" onClick={() => onEdit({ id: f.id, text: f.statement })} disabled={busy === f.id}>
                  <Pencil className="h-3.5 w-3.5" />
                </IconButton>
              )}
              <IconButton label="Forget" danger onClick={() => onForget(f)} disabled={busy === f.id}>
                <Trash2 className="h-3.5 w-3.5" />
              </IconButton>
            </div>
          )}
        </div>
      )}
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
        <span
          className={`rounded-full px-1.5 py-px ${src.mine ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'}`}
          title={src.mine ? 'You said this' : f.confidence ? `An agent concluded this (${f.confidence} confidence)` : 'An agent recorded this'}
        >
          {src.mine ? '' : 'learned by '}
          {src.text}
        </span>
        {u && u.conversations > 0 && u.last_at ? (
          <span className="inline-flex items-center gap-1" title={`Last given to an agent ${new Date(u.last_at).toLocaleString()}`}>
            <Eye className="h-3 w-3" />
            Given to {(u.by ?? []).map((s) => names.get(s) ?? s.toUpperCase()).join(', ')} in{' '}
            {u.conversations === 1 ? '1 conversation' : `${u.conversations} conversations`} · last {relativeAge(u.last_at, new Date(), IN_WORDS)}
          </span>
        ) : (
          <span>Not used yet</span>
        )}
      </div>
    </li>
  )
}
