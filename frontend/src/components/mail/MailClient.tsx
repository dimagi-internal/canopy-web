import { useCallback, useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import { WorkbenchSkeleton } from 'canopy-ui'

import {
  deleteMailDraft,
  deleteMailThread,
  emptyMailFolder,
  fetchInbox,
  fetchMailDrafts,
  fetchMailThread,
  replyToThread,
  saveMailDraft,
  sendMail,
  setMailThreadState,
  type InboxSource,
  type MailDraftOut,
  type MailFolder,
  type MailMessageOut,
  type MailThreadOut,
} from '@/api/mail'
import { parseAddresses } from '@/components/mail/addresses'
import { Composer, mailButton as btn, mailInput as input, mailPrimary as primary, type Draft } from '@/components/mail/Composer'
import { fullDate, mailDate, MessageView } from '@/components/mail/MessageView'
import { isPlainKey, isTypingTarget } from '@/components/work/keys'

// ONE mail client for every inbox: a person's own kept mail (`source.forwarded`) and an agent's
// mailbox (`source.agent`) are the same list, reader, folders, search, reply and compose; they
// differ only in what the server lets them send as. The server decides who may send
// (`canSend` only avoids offering what would 403). The open thread and folder live in the URL.

type Folder = MailFolder | 'drafts'
const FOLDERS: [Folder, string][] = [['inbox', 'Inbox'], ['sent', 'Sent'], ['drafts', 'Drafts'], ['archived', 'Archived'], ['spam', 'Spam'], ['trash', 'Trash']]
const PAGE = 50
const POLL_MS = 30_000

/** How long a sent message waits so it can be undone, and how long new work waits before it saves. */
export const mailTiming = { sendDelayMs: 5000, saveDelayMs: 800, readDelayMs: 1500 }

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

const blankDraft = (patch: Partial<Draft>): Draft => ({
  id: crypto.randomUUID(), threadId: '', from: '', to: '', cc: '', bcc: '', showBcc: false, subject: '', text: '', uploads: [], ...patch,
})

function buildReply(t: MailThreadOut, all: boolean, mine: string[]): Draft {
  const last = newestInbound(t)
  const to = t.default_reply_to.length ? t.default_reply_to : last ? [last.from_addr] : [...(t.messages.at(-1)?.to ?? [])]
  const own = new Set([...mine, t.address.toLowerCase()].concat(t.reply_from ? [t.reply_from.toLowerCase()] : []))
  const cc = all
    ? [...(last?.to ?? []), ...(last?.cc ?? [])]
        .map((a) => a.toLowerCase())
        .filter((a, i, arr) => !own.has(a) && !to.includes(a) && arr.indexOf(a) === i)
    : []
  return blankDraft({ threadId: t.thread_id, to: to.join(', '), cc: cc.join(', '), text: quoted(last ?? t.messages.at(-1)) })
}

function buildForward(t: MailThreadOut, mine: string[]): Draft {
  const m = t.messages.at(-1)
  const base = t.subject.trim()
  const head = m ? `\n\n---------- Forwarded message ----------\nFrom: ${m.from_name ? `${m.from_name} <${m.from_addr}>` : m.from_addr}\nDate: ${new Date(m.created_at).toLocaleString()}\nSubject: ${m.subject}\nTo: ${m.to.join(', ')}\n\n${(m.text || '').trim()}` : ''
  return blankDraft({ from: mine.includes(t.address) ? t.address : mine.length === 1 ? mine[0] : '', subject: /^fwd:/i.test(base) ? base : `Fwd: ${base}`, text: head })
}

const fromSaved = (d: MailDraftOut): Draft => ({
  id: d.id, threadId: d.thread_id, from: d.from_address, to: d.to, cc: d.cc, bcc: d.bcc, showBcc: d.bcc !== '', subject: d.subject, text: d.text, uploads: [...(d.uploads ?? [])],
})

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

type Notice = { text: string; undo?: () => void; error?: boolean }

export function MailClient({ workspace, source, canSend, empty, sendNote, title }: MailClientProps): JSX.Element {
  const [params, setParams] = useSearchParams()
  const openId = params.get('thread')
  const folderParam = params.get('folder') as Folder | null
  const folder: Folder = FOLDERS.some(([f]) => f === folderParam) ? (folderParam as Folder) : 'inbox'

  const [query, setQuery] = useState('')
  const [address, setAddress] = useState('')
  const [threads, setThreads] = useState<MailThreadOut[] | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [drafts, setDrafts] = useState<MailDraftOut[]>([])
  const [unread, setUnread] = useState(0)
  const [addresses, setAddresses] = useState<string[]>([])
  const [agentAddress, setAgentAddress] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [thread, setThread] = useState<MailThreadOut | null>(null)
  const [threadError, setThreadError] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [saved, setSaved] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [saveFailed, setSaveFailed] = useState(false)
  const latest = useRef<Draft | null>(null)
  latest.current = draft
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [cursor, setCursor] = useState<string | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const readDelay = useRef(0)
  const newestRef = useRef<HTMLDivElement>(null)
  const readerRef = useRef<HTMLElement>(null)

  const { agent: sourceAgent, forwarded: sourceForwarded } = source
  const inView = threads?.length ?? 0
  const listSize = useRef(PAGE)
  listSize.current = Math.min(200, Math.max(PAGE, inView))

  const reload = useCallback(async () => {
    const [r, d] = await Promise.all([
      fetchInbox(workspace, { agent: sourceAgent, forwarded: sourceForwarded },
        { folder: folder === 'drafts' ? 'inbox' : folder, q: query, address, limit: listSize.current }),
      canSend ? fetchMailDrafts(workspace, sourceAgent).catch(() => null) : Promise.resolve(null),
    ])
    setThreads((cur) => {
      // A refresh holds the first page; anything "Load more" brought in stays below it.
      const head = new Set(r.items.map((t) => t.thread_id))
      return [...r.items, ...(cur ?? []).slice(r.items.length).filter((t) => !head.has(t.thread_id))]
    })
    setHasMore(r.has_more)
    setUnread(r.unread_total)
    setAddresses(r.addresses)
    setAgentAddress(r.agent_address)
    if (d) setDrafts(d)
    setError(null)
  }, [workspace, sourceAgent, sourceForwarded, canSend, folder, query, address])

  // Changing folder, search or address starts the list over.
  useEffect(() => {
    let alive = true
    setThreads(null)
    setSelected(new Set())
    setCursor(null)
    const t = setTimeout(() => {
      reload().catch((e: unknown) => alive && setError(e instanceof Error ? e.message : 'Failed to load mail'))
    }, query ? 300 : 0)
    return () => { alive = false; clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace, sourceAgent, sourceForwarded, folder, query, address])

  // New mail arrives by itself: a quiet refresh on a timer while the tab is visible, and on return to it.
  useEffect(() => {
    const quiet = () => { if (!document.hidden) reload().catch(() => undefined) }
    const timer = setInterval(quiet, POLL_MS)
    window.addEventListener('focus', quiet)
    document.addEventListener('visibilitychange', quiet)
    return () => {
      clearInterval(timer)
      window.removeEventListener('focus', quiet)
      document.removeEventListener('visibilitychange', quiet)
    }
  }, [reload])

  const go = useCallback((patch: { thread?: string | null; folder?: Folder }, replace = false) => {
    setParams((cur) => {
      const next = new URLSearchParams(cur)
      if (patch.thread !== undefined) { if (patch.thread) next.set('thread', patch.thread); else next.delete('thread') }
      if (patch.folder !== undefined) { if (patch.folder === 'inbox') next.delete('folder'); else next.set('folder', patch.folder) }
      return next
    }, { replace })
  }, [setParams])

  const persist = useCallback((d: Draft) => saveMailDraft(workspace, d.id, {
    agent: sourceAgent ?? '', thread_id: d.threadId, from_address: d.from, to: d.to, cc: d.cc, bcc: d.bcc,
    subject: d.subject, text: d.text, uploads: d.uploads.map((u) => u.id),
  }).then((out) => {
    setDrafts((cur) => [out, ...cur.filter((x) => x.id !== out.id)])
    // Typing may have gone on while this saved: then there is more to save.
    if (latest.current === d) { setDirty(false); setSaved(true) }
    return out
  }), [workspace, sourceAgent])

  // What is typed is saved a moment after the typing stops. Opening a reply and typing nothing saves nothing.
  const edit = useCallback((d: Draft) => { setDraft(d); setDirty(true); setSaved(false) }, [])
  const present = useCallback((d: Draft | null, state: 'clean' | 'saved' | 'dirty' = 'clean') => {
    setDraft(d)
    setDirty(state === 'dirty')
    setSaved(state === 'saved')
  }, [])
  useEffect(() => {
    if (!draft || !dirty) return
    const t = setTimeout(() => {
      persist(draft).then(() => setSaveFailed(false)).catch(() => setSaveFailed(true))
    }, mailTiming.saveDelayMs)
    return () => clearTimeout(t)
  }, [draft, dirty, persist])

  /** Close the composer, keeping what was typed. */
  const closeDraft = useCallback(() => {
    const closing = latest.current
    if (closing && dirty) {
      // It is about to leave the screen, so a failed save has to bring it back rather than lose it.
      persist(closing).catch(() => {
        setDraft((cur) => cur ?? closing)
        setDirty(true)
        setSaveFailed(true)
      })
    }
    present(null)
    setSendError(null)
  }, [persist, present, dirty])

  const open = useCallback((id: string | null, opts: { replace?: boolean; dwell?: boolean } = {}) => {
    readDelay.current = opts.dwell ? mailTiming.readDelayMs : 0
    closeDraft()
    setCursor(id)
    go({ thread: id }, opts.replace)
  }, [closeDraft, go])

  useEffect(() => {
    if (!openId) { setThread(null); return }
    let alive = true
    setThread((cur) => (cur?.thread_id === openId ? cur : null))
    setThreadError(null)
    let timer: ReturnType<typeof setTimeout> | undefined
    fetchMailThread(workspace, openId)
      .then((t) => {
        if (!alive) return
        setThread(t)
        // Moving through mail with j/k only reads what is stopped on.
        if (t.unread) {
          timer = setTimeout(() => {
            setMailThreadState(workspace, openId, { unread: false }).catch(() => undefined)
            setThreads((cur) => cur?.map((x) => (x.thread_id === openId ? { ...x, unread: false } : x)) ?? cur)
            if (!t.archived && !t.spam && !t.trashed) setUnread((n) => Math.max(0, n - 1))
          }, readDelay.current)
        }
        // Open at the newest message: inside the reader pane on a wide screen. On a phone the page
        // itself scrolls, so go to its top, where Back is; older messages are one line each anyway.
        requestAnimationFrame(() => {
          const pane = readerRef.current
          const newest = newestRef.current
          if (pane && newest && pane.scrollHeight > pane.clientHeight) {
            const head = pane.querySelector('[data-reader-head]')?.getBoundingClientRect().height ?? 0
            pane.scrollTop += newest.getBoundingClientRect().top - pane.getBoundingClientRect().top - head
          } else {
            window.scrollTo?.(0, 0)
          }
        })
      })
      .catch((e: unknown) => alive && setThreadError(e instanceof Error ? e.message : 'Failed to load thread'))
    return () => { alive = false; if (timer) clearTimeout(timer) }
  }, [workspace, openId])

  // The open thread follows new mail on it: asked once per change of the listed count, so a count
  // that never matches what the thread holds cannot loop.
  const listedCount = threads?.find((t) => t.thread_id === openId)?.message_count
  useEffect(() => {
    if (!openId || !thread || listedCount === undefined || listedCount === thread.messages.length) return
    fetchMailThread(workspace, openId).then(setThread).catch(() => undefined)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace, openId, listedCount])

  // A saved reply comes back when its thread is opened.
  useEffect(() => {
    if (!thread || draft || !canSend) return
    const mine = drafts.find((d) => d.thread_id === thread.thread_id)
    if (mine) present(fromSaved(mine), 'saved')
    // Only on arriving at a thread: closing a draft must not reopen it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [thread?.thread_id])

  const index = useMemo(() => threads?.findIndex((t) => t.thread_id === (openId ?? cursor)) ?? -1, [threads, openId, cursor])

  const move = useCallback((delta: number) => {
    if (!threads?.length || folder === 'drafts') return
    const next = threads[Math.min(threads.length - 1, Math.max(0, (index < 0 ? (delta > 0 ? -1 : 0) : index) + delta))]
    if (openId) open(next.thread_id, { replace: true, dwell: true })
    else setCursor(next.thread_id)
  }, [threads, index, openId, open, folder])

  const inFolder = folder !== 'drafts'
  const toInbox = folder === 'spam' ? { spam: false } : folder === 'trash' ? { trashed: false } : { archived: false }

  /** Move threads out of this view, offering to put them back. */
  async function moveOut(ids: string[], body: { archived?: boolean; spam?: boolean; trashed?: boolean }, text: string, back: typeof body) {
    if (ids.length === 0) return
    const rest = threads?.filter((t) => !ids.includes(t.thread_id)) ?? []
    const at = threads?.findIndex((t) => t.thread_id === openId) ?? -1
    const after = rest[Math.min(Math.max(at, 0), rest.length - 1)]
    setThreads(rest)
    setSelected(new Set())
    if (openId && ids.includes(openId)) open(after?.thread_id ?? null, { replace: true, dwell: true })
    try {
      await Promise.all(ids.map((id) => setMailThreadState(workspace, id, body)))
      setNotice({
        text,
        undo: () => {
          setNotice(null)
          void Promise.all(ids.map((id) => setMailThreadState(workspace, id, back))).then(reload).catch(() => setNotice({ text: 'Could not undo that', error: true }))
        },
      })
      await reload()
    } catch (e: unknown) {
      setNotice({ text: e instanceof Error ? e.message : 'Could not update that', error: true })
      await reload().catch(() => undefined)
    }
  }

  async function deleteForever(ids: string[]) {
    const n = ids.length
    if (n === 0 || !window.confirm(`Delete ${n === 1 ? 'this conversation' : `${n} conversations`} for good? This cannot be undone.`)) return
    try {
      await Promise.all(ids.map((id) => deleteMailThread(workspace, id)))
      setSelected(new Set())
      if (openId && ids.includes(openId)) open(null, { replace: true })
      await reload()
    } catch (e: unknown) {
      setNotice({ text: e instanceof Error ? e.message : 'Could not delete that', error: true })
    }
  }

  async function emptyFolder() {
    if (folder !== 'trash' && folder !== 'spam') return
    if (!window.confirm(`Delete everything in ${folder === 'trash' ? 'Trash' : 'Spam'} for good? This cannot be undone.`)) return
    try {
      await emptyMailFolder(workspace, folder, sourceAgent)
      open(null, { replace: true })
      await reload()
    } catch (e: unknown) {
      setNotice({ text: e instanceof Error ? e.message : 'Could not empty the folder', error: true })
    }
  }

  const archive = (ids: string[]) => moveOut(ids, { archived: true }, ids.length === 1 ? 'Archived' : `${ids.length} archived`, { archived: false })
  const trash = (ids: string[]) => moveOut(ids, { trashed: true }, ids.length === 1 ? 'Moved to Trash' : `${ids.length} moved to Trash`, { trashed: false })
  const reportSpam = (ids: string[]) => moveOut(ids, { spam: true }, ids.length === 1 ? 'Reported as spam' : `${ids.length} reported as spam`, { spam: false })
  const restore = (ids: string[]) => moveOut(ids, toInbox, ids.length === 1 ? 'Moved to Inbox' : `${ids.length} moved to Inbox`,
    folder === 'spam' ? { spam: true } : folder === 'trash' ? { trashed: true } : { archived: true })

  async function setUnreadOf(ids: string[], value: boolean) {
    if (ids.length === 0) return
    try {
      await Promise.all(ids.map((id) => setMailThreadState(workspace, id, { unread: value })))
      setSelected(new Set())
      if (value && openId && ids.includes(openId)) open(null)
      await reload()
    } catch (e: unknown) {
      setNotice({ text: e instanceof Error ? e.message : 'Could not update that', error: true })
    }
  }

  function startReply(all: boolean) {
    if (!thread) return
    setSendError(null)
    const mine = drafts.find((d) => d.thread_id === thread.thread_id)
    if (mine) present(fromSaved(mine), 'saved')
    else present(buildReply(thread, all, addresses))
  }

  function startCompose() {
    if (addresses.length === 0) return
    open(null)
    setSendError(null)
    present(blankDraft({ from: addresses.length === 1 ? addresses[0] : address }))
  }

  function startForward() {
    if (!thread || addresses.length === 0) return
    const d = buildForward(thread, addresses)
    open(null)
    present(d, 'dirty')
  }

  async function discard() {
    if (!draft) return
    const gone = draft
    present(null)
    setSendError(null)
    setDrafts((cur) => cur.filter((d) => d.id !== gone.id))
    await deleteMailDraft(workspace, gone.id).catch(() => undefined)
    setNotice({
      text: 'Draft discarded',
      undo: () => {
        setNotice(null)
        // Its files went with the draft in storage, so only the words come back.
        present({ ...gone, uploads: [] }, 'dirty')
      },
    })
  }

  // A send waits a few seconds so it can be undone. The draft stays saved until the mail has gone,
  // so closing the tab in that moment loses nothing: it is in Drafts.
  const pending = useRef<{ timer: ReturnType<typeof setTimeout>; run: () => Promise<void> } | null>(null)

  async function deliver(d: Draft) {
    const to = parseAddresses(d.to)
    const cc = parseAddresses(d.cc)
    const bcc = parseAddresses(d.bcc)
    const attachments = d.uploads.map((u) => u.id)
    setSending(true)
    try {
      if (d.threadId === '') {
        const sent = await sendMail(workspace, { agent: sourceAgent, from_address: d.from, to, cc, bcc, attachments, subject: d.subject.trim(), text: d.text })
        if (sent.thread_id) go({ thread: sent.thread_id })
      } else {
        await replyToThread(workspace, d.threadId, { text: d.text, to, cc, bcc, attachments })
        if (openIdRef.current === d.threadId) setThread(await fetchMailThread(workspace, d.threadId))
      }
      setNotice({ text: 'Sent' })
      setDrafts((cur) => cur.filter((x) => x.id !== d.id))
      await deleteMailDraft(workspace, d.id).catch(() => undefined)
      await reload().catch(() => undefined)
    } catch (e: unknown) {
      setNotice(null)
      setSendError(e instanceof Error ? e.message : 'Could not send')
      present(d, 'saved') // back in front of them, with why
    } finally {
      setSending(false)
    }
  }
  const openIdRef = useRef(openId)
  openIdRef.current = openId

  function send() {
    if (!draft) return
    const d = draft
    setSendError(null)
    present(null)
    // Saved first: the delete after sending must land after this write, or the draft comes back.
    const saving = persist(d).catch(() => undefined)
    const run = () => { pending.current = null; return saving.then(() => deliver(d)) }
    if (mailTiming.sendDelayMs <= 0) { void run(); return }
    const timer = setTimeout(() => { void run() }, mailTiming.sendDelayMs)
    pending.current = { timer, run }
    setNotice({
      text: 'Sending…',
      undo: () => {
        clearTimeout(timer)
        pending.current = null
        setNotice(null)
        present(d, 'saved')
      },
    })
  }

  // Leaving the page must not cancel a send already asked for.
  useEffect(() => () => {
    if (pending.current) { clearTimeout(pending.current.timer); void pending.current.run() }
  }, [])

  useEffect(() => {
    if (!notice || notice.error || notice.text === 'Sending…') return
    const t = setTimeout(() => setNotice(null), 8000)
    return () => clearTimeout(t)
  }, [notice])

  function openDraft(d: MailDraftOut) {
    closeDraft()
    go({ thread: d.thread_id || null })
    present(fromSaved(d), 'saved')
  }

  const toggle = (id: string) => setSelected((cur) => {
    const next = new Set(cur)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!isPlainKey(e) || isTypingTarget(e.target)) return
      const target = openId ? [openId] : selected.size ? [...selected] : cursor ? [cursor] : []
      switch (e.key) {
        case 'j': move(1); break
        case 'k': move(-1); break
        case 'Enter': case 'o':
          // Enter on a focused button or link is that control's own.
          if (cursor && !openId && !(e.target as HTMLElement | null)?.closest?.('button, a')) { e.preventDefault(); open(cursor) }
          break
        case 'x': if (cursor) toggle(cursor); break
        case 'c': if (canSend && addresses.length) { e.preventDefault(); startCompose() } break
        case 'r': if (thread && canSend) { e.preventDefault(); startReply(false) } break
        case 'a': if (thread && canSend) { e.preventDefault(); startReply(true) } break
        case 'f': if (thread && canSend) { e.preventDefault(); startForward() } break
        case 'e':
          if (folder === 'inbox' || folder === 'sent') void archive(target)
          else if (folder === 'archived') void restore(target)
          break
        case '#': if (folder === 'trash' || folder === 'spam') void deleteForever(target); else if (inFolder) void trash(target); break
        case '!': if (folder === 'inbox' || folder === 'archived' || folder === 'sent') void reportSpam(target); break
        case 'u': if (inFolder) void setUnreadOf(target, true); break
        case '/': e.preventDefault(); searchRef.current?.focus(); break
        case 'Escape':
          if (draft) closeDraft()
          else if (openId) open(null)
          break
        default: return
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const switchFolder = (f: Folder) => { closeDraft(); setParams(f === 'inbox' ? {} : { folder: f }) }

  const emptyNode = threads && threads.length === 0 && !draft && !query && folder === 'inbox'
    ? empty({ agentAddress, addresses }) : null
  const showAddress = addresses.length > 1 || (threads ?? []).some((t) => t.address !== threads?.[0]?.address)
  const composingNew = !!draft && draft.threadId === ''
  // Below lg there is room for one pane: the reader replaces the list and Back returns to it.
  const reading = !!openId || composingNew
  const selection = [...selected]
  const allSelected = !!threads?.length && threads.every((t) => selected.has(t.thread_id))

  async function loadMore() {
    const r = await fetchInbox(workspace, { agent: sourceAgent, forwarded: sourceForwarded },
      { folder: folder === 'drafts' ? 'inbox' : folder, q: query, address, offset: inView, limit: PAGE })
    setThreads((cur) => {
      const have = new Set((cur ?? []).map((t) => t.thread_id))
      return [...(cur ?? []), ...r.items.filter((t) => !have.has(t.thread_id))]
    })
    setHasMore(r.has_more)
  }

  const threadActions = (ids: string[], one: boolean) => (
    <>
      {(folder === 'inbox' || folder === 'sent') && <button type="button" className={btn} onClick={() => void archive(ids)}>Archive</button>}
      {folder === 'archived' && <button type="button" className={btn} onClick={() => void restore(ids)}>Move to inbox</button>}
      {folder === 'spam' && <button type="button" className={btn} onClick={() => void restore(ids)}>Not spam</button>}
      {folder === 'trash' && <button type="button" className={btn} onClick={() => void restore(ids)}>Restore</button>}
      {(folder === 'spam' || folder === 'trash')
        ? <button type="button" className={btn} onClick={() => void deleteForever(ids)}>Delete forever</button>
        : <button type="button" className={btn} onClick={() => void trash(ids)}>Delete</button>}
      {folder !== 'spam' && folder !== 'trash' && <button type="button" className={btn} onClick={() => void reportSpam(ids)}>Spam</button>}
      {!one && <button type="button" className={btn} onClick={() => void setUnreadOf(ids, false)}>Mark read</button>}
      <button type="button" className={btn} onClick={() => void setUnreadOf(ids, true)}>Mark unread</button>
    </>
  )

  const list = folder === 'drafts' ? (
    drafts.length === 0 ? <p className="text-[13px] text-muted-foreground">No drafts.</p> : (
      <ul className="divide-y divide-border border-y border-border" aria-label="Drafts">
        {drafts.map((d) => (
          <li key={d.id}>
            <button type="button" onClick={() => openDraft(d)} className={`flex w-full flex-col gap-0.5 px-2 py-2 text-left hover:bg-muted ${draft?.id === d.id ? 'bg-muted' : ''}`}>
              <span className="flex items-baseline gap-2">
                <span className="truncate text-[13px] text-foreground">{d.to || '(no recipients)'}</span>
                <time className="ml-auto shrink-0 text-[11px] text-muted-foreground" dateTime={d.updated_at} title={fullDate(d.updated_at)}>{mailDate(d.updated_at)}</time>
              </span>
              <span className="truncate text-[12px] text-muted-foreground">{d.subject || d.thread_subject || '(no subject)'}</span>
            </button>
          </li>
        ))}
      </ul>
    )
  ) : threads === null ? <WorkbenchSkeleton /> : threads.length === 0 ? (
    <p className="text-[13px] text-muted-foreground">
      {query ? 'No mail matches that search.' : folder === 'inbox' ? 'Inbox zero.' : `Nothing in ${FOLDERS.find(([f]) => f === folder)?.[1]}.`}
    </p>
  ) : (
    <>
      <div className="mb-1 flex min-h-8 flex-wrap items-center gap-2 px-2">
        <input type="checkbox" aria-label="Select all" className="h-3.5 w-3.5 accent-primary" checked={allSelected}
          onChange={() => setSelected(allSelected ? new Set() : new Set(threads.map((t) => t.thread_id)))} />
        {selection.length > 0 ? (
          <>
            <span className="text-[12px] text-muted-foreground">{selection.length} selected</span>
            {threadActions(selection, false)}
          </>
        ) : (folder === 'trash' || folder === 'spam') && (
          <>
            <span className="text-[12px] text-muted-foreground">{folder === 'trash' ? 'Deleted after 30 days.' : ''}</span>
            <button type="button" className={btn} onClick={() => void emptyFolder()}>Empty {folder === 'trash' ? 'Trash' : 'Spam'}</button>
          </>
        )}
      </div>
      <ul className="divide-y divide-border border-y border-border" aria-label="Threads">
        {threads.map((t) => {
          const isOpen = t.thread_id === openId
          const name = t.counterpart_name || t.counterpart || t.address
          return (
            <li key={t.thread_id} className={`flex items-start ${isOpen ? 'bg-muted' : ''} ${t.thread_id === cursor && !isOpen ? 'outline outline-1 -outline-offset-1 outline-border' : ''}`}>
              <label className="flex min-h-11 min-w-9 shrink-0 cursor-pointer items-start justify-center pt-3 sm:min-h-0">
                <input type="checkbox" aria-label={`Select ${t.subject || name}`} className="h-3.5 w-3.5 accent-primary"
                  checked={selected.has(t.thread_id)} onChange={() => toggle(t.thread_id)} />
              </label>
              <button type="button" onClick={() => open(t.thread_id)} aria-current={isOpen ? 'true' : undefined}
                className="flex min-w-0 flex-1 flex-col gap-0.5 py-2 pr-2 text-left hover:bg-muted">
                <span className="flex items-baseline gap-2">
                  {t.unread && <span role="img" aria-label="unread" className="inline-block h-2 w-2 shrink-0 rounded-full bg-primary" />}
                  <span className={`truncate text-[13px] text-foreground ${t.unread ? 'font-semibold' : ''}`} title={t.counterpart}>{name}</span>
                  {t.message_count > 1 && <span className="shrink-0 text-[11px] text-muted-foreground">{t.message_count}</span>}
                  {showAddress && <span className="truncate text-[11px] text-muted-foreground" title={t.address}>{t.address.split('@')[0]}@</span>}
                  {t.has_attachments && <span role="img" aria-label="has attachments" title="Has attachments" className="shrink-0 text-[11px] text-muted-foreground">📎</span>}
                  <time className="ml-auto shrink-0 text-[11px] text-muted-foreground" dateTime={t.last_message_at} title={fullDate(t.last_message_at)}>{mailDate(t.last_message_at)}</time>
                </span>
                <span className="truncate text-[12px]">
                  <span className={t.unread ? 'font-medium text-foreground' : 'text-foreground'}>{t.subject || '(no subject)'}</span>
                  {t.snippet && <span className="text-muted-foreground"> — {t.snippet}</span>}
                </span>
              </button>
            </li>
          )
        })}
      </ul>
      {hasMore && (
        <div className="py-2 text-center">
          <button type="button" className={btn} onClick={() => void loadMore()}>Load more</button>
        </div>
      )}
    </>
  )

  const composer = draft && (
    <Composer workspace={workspace} draft={draft} note={sendNote} addresses={addresses} replyFrom={thread?.reply_from ?? ''}
      busy={sending} saved={saved} saveFailed={saveFailed} error={sendError} onChange={edit} onSend={send} onClose={closeDraft} onDiscard={() => void discard()}
      onUploaded={(u) => { setDraft((d) => d && { ...d, uploads: [...d.uploads, u] }); setDirty(true); setSaved(false) }} />
  )

  return (
    <div className="px-4 py-4 sm:px-6 sm:py-6" data-testid="mail-inbox">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {title && <h1 className="mr-2 text-[15px] font-semibold text-foreground">{title}</h1>}
        {canSend && (
          <button type="button" className={primary} onClick={startCompose} disabled={addresses.length === 0}
            title={addresses.length === 0 ? 'There is no address to send from yet (Settings → Email)' : 'Compose (c)'}>
            Compose
          </button>
        )}
        <div role="tablist" aria-label="Folder" className="flex max-w-full overflow-x-auto rounded-md border border-border text-[12px]">
          {FOLDERS.filter(([f]) => f !== 'drafts' || canSend).map(([f, label]) => (
            <button key={f} type="button" role="tab" aria-selected={folder === f} onClick={() => switchFolder(f)}
              className={`min-h-11 shrink-0 px-3 py-1 sm:min-h-0 ${folder === f ? 'bg-muted font-medium text-foreground' : 'text-muted-foreground hover:bg-muted'}`}>
              {label}
              {f === 'inbox' && unread ? ` (${unread})` : ''}
              {f === 'drafts' && drafts.length ? ` (${drafts.length})` : ''}
            </button>
          ))}
        </div>
        {addresses.length > 1 && (
          <select aria-label="Address" className={input} value={address} onChange={(e) => { setAddress(e.target.value); open(null) }}>
            <option value="">All my addresses</option>
            {addresses.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        )}
        <input ref={searchRef} aria-label="Search mail" type="search" placeholder="Search mail" value={query}
          onChange={(e) => { setQuery(e.target.value); if (openId) open(null) }} className={`${input} w-full sm:ml-auto sm:w-64`} />
      </div>

      {error && <p role="alert" className="mb-2 text-[13px] text-destructive">{error}</p>}
      {threads === null && error ? null : emptyNode ? emptyNode : (
        <div className="grid gap-6 lg:h-[calc(100dvh-14rem)] lg:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]">
          <div className={`min-w-0 lg:overflow-y-auto ${reading ? 'hidden lg:block' : ''}`}>{list}</div>

          <section ref={readerRef} aria-label="Reader" className={`min-w-0 lg:overflow-y-auto ${reading ? '' : 'hidden lg:block'}`}>
            {reading && (
              <button type="button" className={`${btn} mb-2 lg:hidden`} onClick={() => { if (composingNew) closeDraft(); if (openId) open(null) }}>
                ← {FOLDERS.find(([f]) => f === folder)?.[1]}
              </button>
            )}
            {composingNew ? (
              <>
                <h2 className="text-[14px] font-semibold text-foreground">New message</h2>
                {composer}
              </>
            ) : !openId ? (
              <p className="text-[13px] text-muted-foreground">Select a message to read it.</p>
            ) : threadError ? (
              <p role="alert" className="text-[13px] text-destructive">{threadError}</p>
            ) : thread === null ? (
              <WorkbenchSkeleton />
            ) : (
              <>
                <div data-reader-head className="sticky top-0 z-10 flex flex-wrap items-center gap-2 bg-background pb-2">
                  <h2 className="mr-auto min-w-0 truncate text-[14px] font-semibold text-foreground" title={thread.subject}>{thread.subject || '(no subject)'}</h2>
                  {canSend && folder !== 'spam' && folder !== 'trash' && (
                    <>
                      <button type="button" className={btn} onClick={() => startReply(false)}>Reply</button>
                      <button type="button" className={btn} onClick={() => startReply(true)}>Reply all</button>
                      <button type="button" className={btn} onClick={startForward} disabled={addresses.length === 0}>Forward</button>
                    </>
                  )}
                  {threadActions([thread.thread_id], true)}
                </div>
                {thread.spam && <p className="mb-2 text-[12px] text-muted-foreground">In Spam: links and attachments may be unsafe.</p>}
                {thread.messages.map((m, i) => {
                  const last = i === thread.messages.length - 1
                  return (
                    <div key={m.id} ref={last ? newestRef : undefined}>
                      <MessageView m={m} initiallyOpen={last} inSpam={thread.spam} />
                    </div>
                  )
                })}
                {composer}
              </>
            )}
          </section>
        </div>
      )}

      {notice && (
        <div role="status" className="fixed bottom-4 left-1/2 z-50 flex -translate-x-1/2 items-center gap-3 rounded-md border border-border bg-card px-3 py-2 text-[13px] text-foreground shadow-lg">
          <span className={notice.error ? 'text-destructive' : ''}>{notice.text}</span>
          {notice.undo && <button type="button" className="font-medium text-primary hover:underline" onClick={notice.undo}>Undo</button>}
          {notice.error && <button type="button" aria-label="Dismiss" className="text-muted-foreground" onClick={() => setNotice(null)}>×</button>}
        </div>
      )}
    </div>
  )
}
