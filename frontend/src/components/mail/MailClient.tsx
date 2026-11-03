import { useCallback, useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from 'react'
import { WorkbenchSkeleton } from 'canopy-ui'

import {
  fetchInbox,
  fetchMailThread,
  replyToThread,
  sendMail,
  setMailThreadState,
  type InboxSource,
  type MailMessageOut,
  type MailThreadOut,
} from '@/api/mail'
import { MessageView } from '@/components/mail/MessageView'
import { relativeAge } from '@/lib/relativeAge'

// ONE mail client for every inbox: a person's own kept mail (`source.forwarded`) and an agent's
// mailbox (`source.agent`) are the same list, reader, search, archive, reply and compose; they
// differ only in what the server lets them send as. The server decides who may send
// (`canSend` only avoids offering what would 403).
//
// Keys: j/k move · c compose · r reply · a reply all · e archive · u unread · / search · Esc close

type View = 'inbox' | 'archived'
type Draft = {
  mode: 'new' | 'reply' | 'replyAll'
  from: string
  to: string
  cc: string
  subject: string
  text: string
}

const btn =
  'min-h-11 rounded-md border border-border px-2.5 py-1 text-[12px] text-foreground hover:bg-muted disabled:opacity-40 sm:min-h-0'
const primary =
  'min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40 sm:min-h-0'
const input = 'rounded-md border border-input bg-input px-2 py-1 text-[13px] text-foreground'

/** "a@x.org, B <b@y.org>; c@z" → ['a@x.org', 'b@y.org', 'c@z'] */
export function parseAddresses(raw: string): string[] {
  const out: string[] = []
  for (const part of raw.split(/[,;\n]+/)) {
    const m = part.match(/<([^<>\s]+@[^<>\s]+)>/) ?? part.trim().match(/^([^\s<>]+@[^\s<>]+)$/)
    const a = m?.[1]?.trim().toLowerCase()
    if (a && !out.includes(a)) out.push(a)
  }
  return out
}

function quoted(m: MailMessageOut | undefined): string {
  if (!m) return ''
  const who = m.from_name || m.from_addr
  const when = new Date(m.created_at).toLocaleString()
  const body = (m.text || '').trim().split('\n').map((l) => `> ${l}`).join('\n')
  return `\n\nOn ${when}, ${who} wrote:\n${body}`
}

function newestInbound(t: MailThreadOut): MailMessageOut | undefined {
  return [...t.messages].reverse().find((m) => m.direction === 'in')
}

function buildReply(t: MailThreadOut, all: boolean, mine: string[]): Draft {
  const last = newestInbound(t)
  const to = t.default_reply_to.length ? t.default_reply_to : last ? [last.from_addr] : []
  const own = new Set([...mine, t.address.toLowerCase()])
  const cc = all
    ? [...(last?.to ?? []), ...(last?.cc ?? [])]
        .map((a) => a.toLowerCase())
        .filter((a, i, arr) => !own.has(a) && !to.includes(a) && arr.indexOf(a) === i)
    : []
  const base = t.subject.trim()
  return {
    mode: all ? 'replyAll' : 'reply',
    from: '',
    to: to.join(', '),
    cc: cc.join(', '),
    subject: /^re:/i.test(base) ? base : `Re: ${base}`,
    text: quoted(last),
  }
}

function isTyping(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable)
}

function Composer({ draft, addresses, busy, error, note, onChange, onSend, onDiscard }: {
  draft: Draft
  note: string
  addresses: string[]
  busy: boolean
  error: string | null
  onChange: (d: Draft) => void
  onSend: () => void
  onDiscard: () => void
}): JSX.Element {
  const isNew = draft.mode === 'new'
  const set = (patch: Partial<Draft>) => onChange({ ...draft, ...patch })
  const ready = parseAddresses(draft.to).length > 0 && draft.text.trim() !== '' && (!isNew || (draft.from !== '' && draft.subject.trim() !== ''))
  return (
    <form
      aria-label={isNew ? 'New message' : 'Reply'}
      className="mt-4 flex flex-col gap-2 rounded-md border border-border p-3"
      onSubmit={(e) => { e.preventDefault(); if (ready && !busy) onSend() }}
      onKeyDown={(e) => {
        if (e.key === 'Escape') onDiscard()
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && ready && !busy) { e.preventDefault(); onSend() }
      }}
    >
      {isNew && (
        <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <span className="w-12">From</span>
          <select aria-label="From" className={`${input} flex-1`} value={draft.from} onChange={(e) => set({ from: e.target.value })}>
            <option value="">Choose an address</option>
            {addresses.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </label>
      )}
      <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
        <span className="w-12">To</span>
        <input aria-label="To" className={`${input} flex-1`} value={draft.to} onChange={(e) => set({ to: e.target.value })}
          placeholder="name@example.com, another@example.com" />
      </label>
      <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
        <span className="w-12">Cc</span>
        <input aria-label="Cc" className={`${input} flex-1`} value={draft.cc} onChange={(e) => set({ cc: e.target.value })} />
      </label>
      {isNew && (
        <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <span className="w-12">Subject</span>
          <input aria-label="Subject" className={`${input} flex-1`} value={draft.subject} onChange={(e) => set({ subject: e.target.value })} />
        </label>
      )}
      <textarea aria-label="Message" rows={isNew ? 10 : 8} className={`${input} w-full font-sans`} value={draft.text}
        onChange={(e) => set({ text: e.target.value })} autoFocus />
      {error && <p role="alert" className="text-[12px] text-destructive">{error}</p>}
      <div className="flex items-center gap-2">
        <button type="submit" className={primary} disabled={!ready || busy}>{busy ? 'Sending…' : 'Send'}</button>
        <button type="button" className={btn} onClick={onDiscard} disabled={busy}>Discard</button>
        <span className="text-[11px] text-muted-foreground">{note} Ctrl/⌘+Enter sends.</span>
      </div>
    </form>
  )
}

export type MailClientProps = {
  workspace: string
  source: InboxSource
  /** May the viewer offer reply / compose (the server still decides). */
  canSend: boolean
  /** Shown instead of the list when the inbox is empty and unsearched; null falls through to "Inbox zero". */
  empty: (info: { agentAddress: string; addresses: string[] }) => ReactNode
  /** One line under the composer saying who the mail goes out as. */
  sendNote: string
  title?: string
}

export function MailClient({ workspace, source, canSend, empty, sendNote, title }: MailClientProps): JSX.Element {
  const [view, setView] = useState<View>('inbox')
  const [query, setQuery] = useState('')
  const [address, setAddress] = useState('')
  const [threads, setThreads] = useState<MailThreadOut[] | null>(null)
  const [unread, setUnread] = useState(0)
  const [addresses, setAddresses] = useState<string[]>([])
  const [agentAddress, setAgentAddress] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [openId, setOpenId] = useState<string | null>(null)
  const [thread, setThread] = useState<MailThreadOut | null>(null)
  const [threadError, setThreadError] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState<string | null>(null)
  const searchRef = useRef<HTMLInputElement>(null)

  const { agent: sourceAgent, forwarded: sourceForwarded } = source
  const reload = useCallback(async () => {
    const r = await fetchInbox(workspace, { agent: sourceAgent, forwarded: sourceForwarded }, { archived: view === 'archived', q: query, address })
    setThreads(r.items)
    setUnread(r.unread_total)
    setAddresses(r.addresses)
    setAgentAddress(r.agent_address)
    setError(null)
  }, [workspace, sourceAgent, sourceForwarded, view, query, address])

  useEffect(() => {
    let alive = true
    const t = setTimeout(() => {
      reload().catch((e: unknown) => alive && setError(e instanceof Error ? e.message : 'Failed to load mail'))
    }, query ? 300 : 0)
    return () => { alive = false; clearTimeout(t) }
  }, [reload, query])

  const open = useCallback((id: string | null) => {
    setOpenId(id)
    setDraft(null)
    setSendError(null)
  }, [])

  useEffect(() => {
    if (!openId) { setThread(null); return }
    let alive = true
    setThread(null)
    setThreadError(null)
    fetchMailThread(workspace, openId)
      .then(async (t) => {
        if (!alive) return
        setThread(t)
        if (t.unread) {
          await setMailThreadState(workspace, openId, { unread: false }).catch(() => undefined)
          if (alive) {
            setThreads((cur) => cur?.map((x) => (x.thread_id === openId ? { ...x, unread: false } : x)) ?? cur)
            setUnread((n) => Math.max(0, n - 1))
          }
        }
      })
      .catch((e: unknown) => alive && setThreadError(e instanceof Error ? e.message : 'Failed to load thread'))
    return () => { alive = false }
  }, [workspace, openId])

  const index = useMemo(() => threads?.findIndex((t) => t.thread_id === openId) ?? -1, [threads, openId])

  const move = useCallback((delta: number) => {
    if (!threads?.length) return
    const next = Math.min(threads.length - 1, Math.max(0, (index < 0 ? (delta > 0 ? -1 : 0) : index) + delta))
    open(threads[next].thread_id)
  }, [threads, index, open])

  async function changeState(body: { unread?: boolean; archived?: boolean }) {
    if (!thread) return
    try {
      await setMailThreadState(workspace, thread.thread_id, body)
      if (body.archived !== undefined) {
        const rest = threads?.filter((t) => t.thread_id !== thread.thread_id) ?? []
        const after = rest[Math.min(index, rest.length - 1)]
        await reload()
        open(after?.thread_id ?? null)
      } else {
        await reload()
        if (body.unread) open(null)
      }
    } catch (e: unknown) {
      setThreadError(e instanceof Error ? e.message : 'Could not update the thread')
    }
  }

  function startReply(all: boolean) {
    if (!thread) return
    setSendError(null)
    setDraft(buildReply(thread, all, addresses))
  }

  function startCompose() {
    setOpenId(null)
    setSendError(null)
    setDraft({ mode: 'new', from: addresses.length === 1 ? addresses[0] : (address || ''), to: '', cc: '', subject: '', text: '' })
  }

  async function send() {
    if (!draft) return
    setSending(true)
    setSendError(null)
    try {
      if (draft.mode === 'new') {
        const sent = await sendMail(workspace, {
          agent: source.agent, from_address: draft.from, to: parseAddresses(draft.to), cc: parseAddresses(draft.cc),
          subject: draft.subject.trim(), text: draft.text,
        })
        setDraft(null)
        await reload()
        if (sent.thread_id) setOpenId(sent.thread_id)
      } else if (thread) {
        await replyToThread(workspace, thread.thread_id, {
          text: draft.text, to: parseAddresses(draft.to), cc: parseAddresses(draft.cc),
        })
        setDraft(null)
        setThread(await fetchMailThread(workspace, thread.thread_id))
        await reload()
      }
    } catch (e: unknown) {
      setSendError(e instanceof Error ? e.message : 'Could not send')
    } finally {
      setSending(false)
    }
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return
      switch (e.key) {
        case 'j': move(1); break
        case 'k': move(-1); break
        case 'c': if (canSend) { e.preventDefault(); startCompose() } break
        case 'r': if (thread && canSend) { e.preventDefault(); startReply(false) } break
        case 'a': if (thread && canSend) { e.preventDefault(); startReply(true) } break
        case 'e': if (thread) void changeState({ archived: view === 'inbox' }); break
        case 'u': if (thread && view === 'inbox') void changeState({ unread: true }); break
        case '/': e.preventDefault(); searchRef.current?.focus(); break
        case 'Escape': if (draft) setDraft(null); else if (openId) open(null); break
        default: return
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const emptyNode = threads && threads.length === 0 && !draft && !query && view === 'inbox'
    ? empty({ agentAddress, addresses }) : null
  const showAddress = addresses.length > 1 || (threads ?? []).some((t) => t.address !== threads?.[0]?.address)

  return (
    <div className="px-6 py-6" data-testid="mail-inbox">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {title && <h1 className="mr-2 text-[15px] font-semibold text-foreground">{title}</h1>}
        {canSend && (
          <button type="button" className={primary} onClick={startCompose} disabled={addresses.length === 0}
            title={addresses.length === 0 ? 'There is no address to send from yet (Settings → Email)' : 'Compose (c)'}>
            Compose
          </button>
        )}
        <div role="tablist" aria-label="Folder" className="flex overflow-hidden rounded-md border border-border text-[12px]">
          {(['inbox', 'archived'] as const).map((v) => (
            <button key={v} type="button" role="tab" aria-selected={view === v}
              onClick={() => { setView(v); open(null) }}
              className={`min-h-11 px-3 py-1 sm:min-h-0 ${view === v ? 'bg-muted font-medium text-foreground' : 'text-muted-foreground hover:bg-muted'}`}>
              {v === 'inbox' ? `Inbox${unread ? ` (${unread})` : ''}` : 'Archived'}
            </button>
          ))}
        </div>
        {addresses.length > 1 && (
          <select aria-label="Address" className={input} value={address} onChange={(e) => { setAddress(e.target.value); open(null) }}>
            <option value="">All my addresses</option>
            {addresses.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        )}
        <input ref={searchRef} aria-label="Search mail" type="search" placeholder="Search mail ( / )" value={query}
          onChange={(e) => { setQuery(e.target.value); open(null) }} className={`${input} ml-auto w-64`} />
      </div>

      {error ? (
        <p role="alert" className="text-[13px] text-destructive">{error}</p>
      ) : threads === null ? (
        <WorkbenchSkeleton />
      ) : emptyNode ? (
        emptyNode
      ) : (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]">
          <div className="min-w-0">
            {threads.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">
                {query ? 'No mail matches that search.' : view === 'archived' ? 'Nothing archived.' : 'Inbox zero.'}
              </p>
            ) : (
              <ul className="divide-y divide-border border-y border-border" aria-label="Threads">
                {threads.map((t) => (
                  <li key={t.thread_id}>
                    <button type="button" onClick={() => open(t.thread_id)} aria-current={t.thread_id === openId ? 'true' : undefined}
                      className={`flex w-full flex-col gap-0.5 px-2 py-2 text-left hover:bg-muted ${t.thread_id === openId ? 'bg-muted' : ''}`}>
                      <span className="flex items-baseline gap-2">
                        {t.unread && <span aria-label="unread" className="inline-block h-2 w-2 shrink-0 rounded-full bg-primary" />}
                        <span className={`truncate text-[13px] ${t.unread ? 'font-semibold text-foreground' : 'text-foreground'}`}>
                          {t.counterpart || t.address}
                        </span>
                        <time className="ml-auto shrink-0 text-[11px] text-muted-foreground" dateTime={t.last_message_at}>
                          {relativeAge(t.last_message_at)}
                        </time>
                      </span>
                      <span className={`truncate text-[12px] ${t.unread ? 'text-foreground' : 'text-muted-foreground'}`}>
                        {t.subject || '(no subject)'}
                        {t.message_count > 1 && <span className="ml-1 text-muted-foreground">({t.message_count})</span>}
                      </span>
                      {showAddress && <span className="truncate text-[11px] text-muted-foreground">to {t.address}</span>}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <section aria-label="Reader" className="min-w-0">
            {draft && draft.mode === 'new' ? (
              <>
                <h2 className="text-[14px] font-semibold text-foreground">New message</h2>
                <Composer draft={draft} note={sendNote} addresses={addresses} busy={sending} error={sendError}
                  onChange={setDraft} onSend={() => void send()} onDiscard={() => setDraft(null)} />
              </>
            ) : !openId ? (
              <p className="text-[13px] text-muted-foreground">Select a message to read it{canSend ? ', or press c to compose' : ''}.</p>
            ) : threadError ? (
              <p role="alert" className="text-[13px] text-destructive">{threadError}</p>
            ) : thread === null ? (
              <WorkbenchSkeleton />
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="mr-auto min-w-0 truncate text-[14px] font-semibold text-foreground">{thread.subject || '(no subject)'}</h2>
                  {canSend && <button type="button" className={btn} onClick={() => startReply(false)}>Reply</button>}
                  {canSend && <button type="button" className={btn} onClick={() => startReply(true)}>Reply all</button>}
                  <button type="button" className={btn} onClick={() => void changeState({ archived: view === 'inbox' })}>
                    {view === 'inbox' ? 'Archive' : 'Move to inbox'}
                  </button>
                  {view === 'inbox' && (
                    <button type="button" className={btn} onClick={() => void changeState({ unread: true })}>Mark unread</button>
                  )}
                </div>
                {thread.messages.map((m) => <MessageView key={m.id} m={m} />)}
                {draft && (
                  <Composer draft={draft} note={sendNote} addresses={addresses} busy={sending} error={sendError}
                    onChange={setDraft} onSend={() => void send()} onDiscard={() => setDraft(null)} />
                )}
              </>
            )}
          </section>
        </div>
      )}
    </div>
  )
}
