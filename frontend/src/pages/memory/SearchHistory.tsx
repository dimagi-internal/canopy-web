import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronRight, ExternalLink } from 'lucide-react'

import { listMySearches, previewMySearch } from '../../api/people'
import type { SearchPreview as Preview, SearchRecord } from '../../api/people'
import { EntryScore, SearchPreview } from './SearchPreview'

/** The purpose canopy gives a turn's own look-up (apps/contacts/people.py ENVELOPE_PURPOSE). */
const TURN_START = "answer this person's turn: context about who is asking"

/** "How agents searched it": every real search over your memory, newest first,
 *  as it ran — the question, what came back and why, what was left out and why
 *  not, the cutoff then — with the session it served. Searches from before
 *  scores were kept show what came back only. */
export function SearchHistory() {
  const [rows, setRows] = useState<readonly SearchRecord[]>([])
  const [total, setTotal] = useState<number | null>(null)
  const [next, setNext] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = async (before?: string) => {
    setLoading(true)
    try {
      const page = await listMySearches(before)
      setRows((r) => (before ? [...r, ...page.searches] : page.searches))
      if (!before) setTotal(page.total ?? null)
      setNext(page.next_before ?? null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => {
    void load()
  }, [])

  const oldest = rows.length ? rows[rows.length - 1].at : null
  return (
    <section id="searches" aria-label="How agents searched it" className="scroll-mt-6">
      <h2 className="text-lg font-semibold text-foreground">How agents searched it</h2>
      <p className="text-sm text-muted-foreground">
        Every time an agent looked something up in your memory: what it asked, what came back and why, and what was left out.
        {total != null && total > 0 && ` ${total} searches so far.`}
      </p>
      {error && (
        <p role="alert" className="mt-2 text-sm text-destructive">
          {error}
        </p>
      )}
      {!loading && rows.length === 0 && !error && <p className="mt-3 text-sm text-muted-foreground">No agent has searched it yet.</p>}

      <ol className="mt-3 divide-y divide-border rounded-xl border border-border">
        {rows.map((s) => (
          <SearchRow key={s.retrieval_id} s={s} open={open === s.retrieval_id} toggle={() => setOpen(open === s.retrieval_id ? null : s.retrieval_id)} />
        ))}
      </ol>
      {next && (
        <button className="mt-3 text-sm text-primary hover:underline disabled:opacity-50" disabled={loading} onClick={() => void load(next)}>
          {loading ? 'Loading…' : `Show earlier (before ${oldest ? new Date(oldest).toLocaleDateString() : ''})`}
        </button>
      )}

      <SearchPreview />
    </section>
  )
}

function who(s: SearchRecord): string {
  if (s.agent) return s.agent
  if (s.actor_type === 'system') return 'Canopy'
  if (s.actor_type === 'user') return 'You'
  const slug = s.actor_id.replace(/^agent:/, '')
  return slug.charAt(0).toUpperCase() + slug.slice(1)
}

function SearchRow({ s, open, toggle }: { s: SearchRecord; open: boolean; toggle: () => void }) {
  const back = s.entries.filter((e) => e.position != null)
  const hybrid = s.method === 'hybrid'
  const when = new Date(s.at)
  return (
    <li>
      <button type="button" onClick={toggle} aria-expanded={open} className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-muted/40">
        {open ? <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" /> : <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />}
        <span className="w-28 shrink-0 text-xs leading-5 text-muted-foreground">
          {when.toLocaleDateString([], { month: 'short', day: 'numeric' })}{' '}
          {when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-foreground">
            <span className="font-medium">{who(s)}</span>{' '}
            <span className="text-muted-foreground">asked</span> {s.query ? `“${s.query.slice(0, 140)}”` : <span className="italic text-muted-foreground">(question not recorded)</span>}
          </span>
          <span className="block text-xs text-muted-foreground">
            {back.length === 0 ? 'nothing came back' : `${back.length} came back`}
            {s.scored && ` of ${s.entries.length} scored`}
            {!s.scored && ' · scores not kept then'}
          </span>
        </span>
      </button>
      {open && <SearchDetail s={s} hybrid={hybrid} />}
    </li>
  )
}

function SearchDetail({ s, hybrid }: { s: SearchRecord; hybrid: boolean }) {
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
            This ran before canopy kept scores, so here is what came back, in order, but not how close each was or what was left out.
          </p>
          <ol className="space-y-2">
            {s.entries.map((e, i) => (
              <EntryScore key={e.entry_id ?? i} e={e} cutoff={null} scored={false} />
            ))}
          </ol>
          {s.query && !today && (
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
