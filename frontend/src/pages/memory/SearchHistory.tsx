import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronRight, ExternalLink } from 'lucide-react'

import { listMySearches, previewMySearch } from '../../api/people'
import type { SearchPreview as Preview, SearchRecord } from '../../api/people'
import { EntryScore, SearchPreview } from './SearchPreview'

/** The purpose canopy gives a turn's own look-up (apps/contacts/people.py ENVELOPE_PURPOSE). */
const TURN_START = "answer this person's turn: context about who is asking"

/** One conversation's look-ups, newest first. Before 2026-10-18 a turn's context
 *  was fetched (and logged) several times — the same question, all but one empty —
 *  so a turn is one row, shown by the look-up that found the most. */
export type Conversation = { key: string; best: SearchRecord; runs: number }

export function byConversation(rows: readonly SearchRecord[]): Conversation[] {
  const out: Conversation[] = []
  const at = new Map<string, Conversation>()
  for (const s of rows) {
    const key = s.turn_id ?? s.retrieval_id
    const c = at.get(key)
    if (!c) {
      const row = { key, best: s, runs: 1 }
      at.set(key, row)
      out.push(row)
      continue
    }
    c.runs += 1
    if (returned(s) > returned(c.best)) c.best = s
  }
  return out
}

const returned = (s: SearchRecord) => s.entries.filter((e) => e.position != null).length

/** "Conversations that used it": each conversation in which an agent looked you
 *  up — who, what you had asked, and what it was given — with the session it
 *  served. Scores and what was left out stay one click away. */
export function SearchHistory({
  fetchPage = listMySearches,
  names,
  canPreview = true,
}: {
  fetchPage?: (before?: string) => Promise<{ searches: readonly SearchRecord[]; next_before?: string | null }>
  names?: Map<string, string>
  canPreview?: boolean
}) {
  const [rows, setRows] = useState<readonly SearchRecord[]>([])
  const [next, setNext] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = async (before?: string) => {
    setLoading(true)
    try {
      const page = await fetchPage(before)
      setRows((r) => (before ? [...r, ...page.searches] : page.searches))
      setNext(page.next_before ?? null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const convos = byConversation(rows)
  const oldest = rows.length ? rows[rows.length - 1].at : null
  return (
    <section id="conversations" aria-label="Conversations that used it" className="scroll-mt-6">
      <h2 className="text-lg font-semibold text-foreground">Conversations that used it</h2>
      <p className="text-sm text-muted-foreground">What you asked, and what the agent was given about you to answer it.</p>
      {error && (
        <p role="alert" className="mt-2 text-sm text-destructive">
          {error}
        </p>
      )}
      {!loading && rows.length === 0 && !error && <p className="mt-3 text-sm text-muted-foreground">No agent has used it yet.</p>}

      {convos.length > 0 && (
        <ol className="mt-3 divide-y divide-border rounded-xl border border-border">
          {convos.map((c) => (
            <SearchRow
              key={c.key}
              s={c.best}
              names={names}
              canPreview={canPreview}
              open={open === c.key}
              toggle={() => setOpen(open === c.key ? null : c.key)}
            />
          ))}
        </ol>
      )}
      {next && (
        <button className="mt-3 text-sm text-primary hover:underline disabled:opacity-50" disabled={loading} onClick={() => void load(next)}>
          {loading ? 'Loading…' : `Show earlier (before ${oldest ? new Date(oldest).toLocaleDateString([], { month: 'short', day: 'numeric' }) : ''})`}
        </button>
      )}
    </section>
  )
}

/** "Try a question" — score a question against your memory the way an agent's
 *  search would. A tool for checking the ranking, so it lives under Advanced. */
export { SearchPreview }

function who(s: SearchRecord, names?: Map<string, string>): string {
  if (s.agent) return s.agent
  if (s.actor_type === 'system') return 'Canopy'
  if (s.actor_type === 'user') return 'You'
  const slug = s.actor_id.replace(/^agent:/, '').split('/')[0]
  return names?.get(slug) ?? slug.charAt(0).toUpperCase() + slug.slice(1)
}

/** A turn's question as the person typed it: a slash command's arguments are
 *  machinery ("/ace:turn --thread 1a15…"), so it reads as the command alone. */
function asked(q: string): string {
  const t = q.trim()
  if (t.startsWith('/')) return t.split(/\s+/)[0]
  return t.length > 140 ? `${t.slice(0, 140)}…` : t
}

function SearchRow({
  s,
  names,
  canPreview,
  open,
  toggle,
}: {
  s: SearchRecord
  names?: Map<string, string>
  canPreview: boolean
  open: boolean
  toggle: () => void
}) {
  const back = s.entries.filter((e) => e.position != null)
  const hybrid = s.method === 'hybrid'
  const when = new Date(s.at)
  return (
    <li>
      <button type="button" onClick={toggle} aria-expanded={open} className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-muted/40">
        {open ? <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" /> : <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />}
        <span className="w-24 shrink-0 text-xs leading-5 text-muted-foreground">
          {when.toLocaleDateString([], { month: 'short', day: 'numeric' })}{' '}
          {when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-foreground">
            <span className="font-medium">{who(s, names)}</span>{' '}
            {s.query ? <span className="text-muted-foreground">— “{asked(s.query)}”</span> : null}
          </span>
          <span className="block text-xs text-muted-foreground">
            {back.length === 0
              ? 'Nothing about you was relevant'
              : `Given ${back.length === 1 ? '1 thing' : `${back.length} things`} about you`}
          </span>
        </span>
      </button>
      {open && <SearchDetail s={s} hybrid={hybrid} canPreview={canPreview} />}
    </li>
  )
}

function SearchDetail({ s, hybrid, canPreview }: { s: SearchRecord; hybrid: boolean; canPreview: boolean }) {
  const [full, setFull] = useState(false)
  const [today, setToday] = useState<Preview | null>(null)
  const [busy, setBusy] = useState(false)
  const long = s.query.length > 400
  return (
    <div className="space-y-3 px-4 pb-4 pl-11">
      <div className="text-xs text-muted-foreground">
        <span>{s.purpose === TURN_START ? 'Looked you up at the start of a turn, using its prompt as the question.' : `Searched to ${s.purpose.replace(/^to\s+/i, '')}.`}</span>{' '}
        {s.session_href && (
          <Link to={s.session_href} className="inline-flex items-center gap-1 text-primary hover:underline">
            Open the session{s.session_title ? ` “${s.session_title}”` : ''} <ExternalLink className="h-3 w-3" />
          </Link>
        )}
      </div>
      {s.query && (
        <div className="whitespace-pre-wrap rounded-md bg-muted/50 px-3 py-2 text-xs text-foreground">
          {full || !long ? s.query : `${s.query.slice(0, 400)}…`}
          {long && (
            <button className="ml-1 text-primary hover:underline" onClick={() => setFull(!full)}>
              {full ? 'less' : 'all of it'}
            </button>
          )}
        </div>
      )}
      {s.scored ? (
        <>
          <p className="text-xs text-muted-foreground">
            {hybrid
              ? `Words and meaning (${s.model.replace(/^vertex\//, '')}), cutoff ${s.floor?.toFixed(2)}${s.embed_ms != null ? `, ${s.embed_ms} ms` : ''}.`
              : 'Shared words only — search by meaning was off.'}{' '}
            Every entry it could reach is below, the ones that came back numbered.
          </p>
          <ol className="space-y-3">
            {s.entries.map((e, i) => (
              <EntryScore key={e.entry_id ?? i} e={e} cutoff={hybrid ? (s.floor ?? null) : null} />
            ))}
          </ol>
        </>
      ) : (
        <>
          <p className="text-xs text-muted-foreground">
            An older conversation: this is what the agent was given, in order. How each was chosen wasn’t kept then.
          </p>
          <ol className="space-y-2">
            {s.entries.map((e, i) => (
              <EntryScore key={e.entry_id ?? i} e={e} cutoff={null} scored={false} />
            ))}
          </ol>
          {canPreview && s.query && !today && (
            <button
              className="text-xs text-primary hover:underline disabled:opacity-50"
              disabled={busy}
              onClick={async () => {
                setBusy(true)
                try {
                  setToday(await previewMySearch(s.query.slice(0, 500)))
                } finally {
                  setBusy(false)
                }
              }}
            >
              {busy ? 'Scoring…' : 'Score this question with today’s memory and settings'}
            </button>
          )}
          {today && (
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
              <p className="mb-2 text-xs text-amber-700 dark:text-amber-400">
                Scored today, against what your memory holds now — not what happened then.
              </p>
              <ol className="space-y-3">
                {today.entries.map((e, i) => (
                  <EntryScore key={e.entry_id ?? i} e={e} cutoff={today.method === 'hybrid' ? today.floor : null} />
                ))}
              </ol>
            </div>
          )}
        </>
      )}
    </div>
  )
}
