/**
 * `canopy.google`: Google operations a page asks for by name, run in the page's MODE.
 *
 * Two modes, chosen by the person, never by a failure:
 *   - `local`:  the tab beside the browser extension's panel, through its site adapter
 *               (`canopy.browser`, the `BrowserGate`). Nothing leaves the browser.
 *   - `server`: the viewer's Google through canopy-web (`canopy.connection`, `ConnectionCalls`).
 *
 * An operation lists the modes it has (`GOOGLE_OPS`). In a mode it lacks it is refused `not_in_mode`;
 * a local operation whose subject is not on screen is refused `not_on_screen`. Neither is ever retried
 * in the other mode: which way a call went is the person's choice, and every answer says which it was.
 * Both modes answer the same shape, so a page written once runs in either.
 *
 * Every gate is the one the underlying call already passes: a local call is a declared adapter
 * capability on a matching tab (an act needs a click); a server call is a declared connection call
 * (a write needs a click's lease). This module only chooses between them and shapes the answers.
 * Design: docs/architecture/local-and-server-modes.md.
 */
import type { CallResult } from './connectionCalls'

export type Mode = 'local' | 'server'
export const MODES: readonly Mode[] = ['local', 'server']

export interface OpSpec {
  /** The modes this operation runs in. */
  modes: readonly Mode[]
  /** Whether it changes something (the page, or the mailbox): it needs a click either way. */
  act: boolean
  /** Local: the adapter and capability it uses (the page must declare it). */
  local?: { adapter: string; capability: string }
  /** Server: the Google call(s) it makes (the page must declare them). */
  server?: readonly string[]
  summary: string
}

/** The catalogue. Adding a mode to an operation is adding its implementation below, never a fallback. */
export const GOOGLE_OPS: Readonly<Record<string, OpSpec>> = {
  'gmail.thread': {
    modes: ['local', 'server'], act: false,
    local: { adapter: 'gmail', capability: 'read_open_thread' },
    server: ['GET gmail/threads/*'],
    summary: 'Every message of a thread, oldest first. Local: the thread open on screen.',
  },
  'gmail.draft': {
    modes: ['local', 'server'], act: true,
    local: { adapter: 'gmail', capability: 'start_compose' },
    server: ['GET gmail/threads/*', 'POST gmail/drafts'],
    summary: 'A new email or a reply, never sent. Local: an open compose window on screen. Server: a saved draft.',
  },
  'gmail.open_email': {
    modes: ['local'], act: false,
    local: { adapter: 'gmail', capability: 'read_open_email' },
    summary: 'The email open on screen.',
  },
  'gmail.fill_compose': {
    modes: ['local'], act: true,
    local: { adapter: 'gmail', capability: 'fill_compose' },
    summary: 'Write into the compose window on screen.',
  },
  'gmail.compose_recipients': {
    modes: ['local'], act: false,
    local: { adapter: 'gmail', capability: 'compose_recipients' },
    summary: 'Who the compose window on screen is addressed to.',
  },
}

/** What the page may see of the catalogue: each operation's modes, whether it acts, what it says. */
export function opsCatalogue(): Record<string, { modes: Mode[]; act: boolean; summary: string }> {
  return Object.fromEntries(Object.entries(GOOGLE_OPS).map(([k, v]) => [k, { modes: [...v.modes], act: v.act, summary: v.summary }]))
}

export class OpError extends Error {
  code: string
  constructor(code: string, message: string) {
    super(message)
    this.code = code
  }
}

export interface OpHost {
  mode: Mode
  /** The tab's adapter through the panel's gate; absent outside the panel. `clicked` is read by the caller. */
  local?: { request(capability: string, args: Record<string, unknown>, clicked: boolean): Promise<unknown> }
  /** The page's connection calls (`ConnectionCalls`): `call` as `canopy.connection.request`, `begin` a click's lease. */
  server: { call(request: Record<string, unknown>): Promise<CallResult>; begin(): Promise<unknown> }
  /** A click on the page, read when the request arrived. */
  clicked: boolean
  /** The signed-in viewer's address: who "me" is when working out a reply's recipients. */
  viewerEmail: string
}

export interface Person { name: string; email: string }
export interface ThreadMessage {
  emailId: string
  from: Person | null
  to: Person[]
  cc: Person[]
  date: string
  /** false when the viewer sent it, true when someone else did, null when unknown. */
  incoming: boolean | null
  bodyText: string
  /** false when `bodyText` is a snippet or was cut. */
  bodyComplete: boolean
}
export interface Thread {
  mode: Mode
  threadId: string
  subject: string
  gmailUrl: string
  messages: ThreadMessage[]
}
export interface Draft {
  mode: Mode
  /** `compose`: open on screen, unsaved, for the person to send. `drafts`: saved in their Drafts. */
  where: 'compose' | 'drafts'
  /** What the draft holds: `html` only on the server from `bodyHtml`; the compose on screen takes text. */
  format: 'text' | 'html'
  kind: DraftKind
  draftId: string | null
  threadId: string | null
}
type DraftKind = 'new' | 'reply' | 'reply_all'

const MAX_BODY = 50_000
const MAX_HTML = 200_000
const MAX_RECIPIENTS = 50
const NOT_ON_SCREEN_THREAD = 'Open that thread in Gmail beside the panel, or switch to server mode.'

/**
 * Run `name` with `args` in `host.mode`. Everything up to the first await runs in the message event,
 * so a click is still live for the call that needs it.
 */
export function runGoogleOp(name: unknown, args: unknown, host: OpHost): Promise<unknown> {
  try {
    const spec = typeof name === 'string' ? GOOGLE_OPS[name] : undefined
    if (!spec) throw new OpError('unknown_op', `canopy.google has no operation "${String(name)}".`)
    const a = (args && typeof args === 'object' && !Array.isArray(args) ? args : {}) as Record<string, unknown>
    if (!spec.modes.includes(host.mode)) {
      const other = spec.modes[0]
      throw new OpError('not_in_mode', `"${name}" works only in ${other} mode; this page is in ${host.mode} mode.`)
    }
    if (host.mode === 'local') {
      if (!host.local) throw new OpError('not_in_mode', 'Local mode needs the Dimagi Canopy extension\'s panel.')
      return LOCAL[name as string](a, host.local, host.clicked)
    }
    return SERVER[name as string](a, host)
  } catch (e) {
    return Promise.reject(e)
  }
}

// ---- local ---------------------------------------------------------------------------------------

type Local = NonNullable<OpHost['local']>
const LOCAL: Record<string, (a: Record<string, unknown>, tab: Local, clicked: boolean) => Promise<unknown>> = {
  'gmail.thread': async (a, tab) => {
    const want = a.threadId == null ? '' : hexId(String(a.threadId))
    const got = await tab.request('read_open_thread', {}, false) as LocalThread | null
    if (!got) throw new OpError('not_on_screen', 'No thread is open in Gmail beside the panel.')
    if (want && hexId(got.threadId) !== want) throw new OpError('not_on_screen', NOT_ON_SCREEN_THREAD)
    return localThread(got)
  },
  'gmail.draft': (a, tab, clicked) => {
    const d = draftArgs(a)
    const send = (): Promise<Draft> => tab.request('start_compose', {
      kind: d.kind,
      ...(d.to.length ? { to: d.to } : {}), ...(d.cc.length ? { cc: d.cc } : {}), ...(d.bcc.length ? { bcc: d.bcc } : {}),
      ...(d.subject != null ? { subject: d.subject } : {}), bodyText: d.bodyText,
    }, clicked).then(() => ({ mode: 'local', where: 'compose', format: 'text', kind: d.kind, draftId: null, threadId: d.threadId || null }))
    if (d.kind === 'new' || !d.threadId) return send()
    // A reply is to the thread on screen: say so rather than reply to a different one.
    return tab.request('read_open_thread', {}, false).then((t) => {
      const open = t as LocalThread | null
      if (!open || hexId(open.threadId) !== hexId(d.threadId)) throw new OpError('not_on_screen', NOT_ON_SCREEN_THREAD)
      return send()
    })
  },
  'gmail.open_email': (_a, tab) => tab.request('read_open_email', {}, false),
  'gmail.fill_compose': (a, tab, clicked) => {
    const bodyText = text(a.bodyText, 'bodyText')
    const mode = a.mode === 'replace' ? 'replace' : 'insert'
    return tab.request('fill_compose', { bodyText, mode, ...(a.subject != null ? { subject: String(a.subject) } : {}) }, clicked)
      .then((r) => {
        if (r == null) throw new OpError('not_on_screen', 'No compose window is open in Gmail beside the panel.')
        return r
      })
  },
  'gmail.compose_recipients': (_a, tab) => tab.request('compose_recipients', {}, false),
}

interface LocalPerson { name?: string; email?: string }
interface LocalThread {
  threadId: string
  subject?: string
  gmailUrl?: string | null
  messages?: {
    emailId?: string; messageId?: string; from?: LocalPerson | null; to?: LocalPerson[]; cc?: LocalPerson[]
    date?: string; incoming?: boolean | null; bodyText?: string; bodyComplete?: boolean
  }[]
}

function localThread(t: LocalThread): Thread {
  const threadId = hexId(t.threadId)
  return {
    mode: 'local',
    threadId,
    subject: t.subject ?? '',
    gmailUrl: t.gmailUrl || gmailUrl(threadId),
    messages: (t.messages ?? []).map((m) => ({
      emailId: m.emailId || m.messageId || '',
      from: m.from?.email ? person(m.from) : null,
      to: (m.to ?? []).filter((p) => p?.email).map(person),
      cc: (m.cc ?? []).filter((p) => p?.email).map(person),
      date: isoDate(m.date),
      incoming: m.incoming ?? null,
      bodyText: m.bodyText ?? '',
      bodyComplete: m.bodyComplete !== false,
    })),
  }
}

// ---- server --------------------------------------------------------------------------------------

const SERVER: Record<string, (a: Record<string, unknown>, host: OpHost) => Promise<unknown>> = {
  'gmail.thread': async (a, host) => {
    const id = gmailId(a.threadId)
    return serverThread(await getThread(host, id, 'full'), host.viewerEmail)
  },
  'gmail.draft': (a, host) => {
    const d = draftArgs(a)
    // The click opens the lease NOW; the reads a reply needs then run on it, and the write after them.
    const lease = host.server.begin()
    return lease.then(async () => {
      let to = d.to, cc = d.cc, subject = d.subject, threadId: string | null = null
      const headers: Record<string, string> = {}
      if (d.kind !== 'new') {
        if (!d.threadId) throw new OpError('bad_request', 'A reply needs the threadId of the thread it answers.')
        threadId = gmailId(d.threadId)
        const t = await getThread(host, threadId, 'metadata')
        const all = t.messages ?? []
        const last = all.at(-1)
        if (!last) throw new OpError('bad_request', 'That thread has no messages to reply to.')
        const h = headerMap(last)
        const reply = replyRecipients(d.kind, h, host.viewerEmail)
        if (!to.length) to = reply.to
        if (!cc.length && d.kind === 'reply_all') cc = reply.cc
        if (subject == null) {
          // The thread's subject: the latest message's, else the newest one that has one.
          const was = [...all].reverse().map((m) => headerMap(m).subject).find((x) => x) ?? ''
          subject = !was || /^re:/i.test(was) ? was : `Re: ${was}`
        }
        if (h['message-id']) {
          headers['In-Reply-To'] = h['message-id']
          headers.References = [h.references, h['message-id']].filter(Boolean).join(' ')
        }
      }
      const raw = mime({ to, cc, bcc: d.bcc, subject: subject ?? '', body: d.bodyText, html: d.bodyHtml, headers })
      const res = await host.server.call({
        provider: 'google', method: 'POST', path: 'gmail/drafts',
        body: { message: { raw, ...(threadId ? { threadId } : {}) } },
      })
      const body = google(res) as { id?: string; message?: { threadId?: string } }
      const out: Draft = { mode: 'server', where: 'drafts', format: d.bodyHtml != null ? 'html' : 'text', kind: d.kind, draftId: body.id ?? null, threadId: body.message?.threadId ?? threadId }
      return out
    })
  },
}

interface ApiPart { mimeType?: string; body?: { data?: string }; parts?: ApiPart[]; headers?: { name: string; value: string }[] }
interface ApiMessage { id: string; threadId?: string; labelIds?: string[]; snippet?: string; internalDate?: string; payload?: ApiPart }
interface ApiThread { id: string; messages?: ApiMessage[] }

async function getThread(host: OpHost, id: string, format: 'full' | 'metadata'): Promise<ApiThread> {
  const query: Record<string, string | string[]> = { format }
  if (format === 'metadata') query.metadataHeaders = ['From', 'To', 'Cc', 'Reply-To', 'Subject', 'Message-ID', 'References']
  const res = await host.server.call({ provider: 'google', method: 'GET', path: `gmail/threads/${id}`, query })
  return google(res) as ApiThread
}

/** Google's answer, or an `OpError` naming what Google said. */
function google(res: CallResult): unknown {
  if (res.status >= 200 && res.status < 300) return res.body
  const said = (res.body as { error?: { message?: string } } | undefined)?.error?.message
  throw new OpError(res.status === 404 ? 'not_found' : 'provider_error', said || `Google answered ${res.status}.`)
}

export function serverThread(t: ApiThread, viewerEmail: string): Thread {
  const me = viewerEmail.toLowerCase()
  const messages = (t.messages ?? []).map((m) => {
    const h = headerMap(m)
    const from = parseAddresses(h.from ?? '')[0] ?? null
    const sent = (m.labelIds ?? []).includes('SENT') || (!!me && from?.email.toLowerCase() === me)
    let body = bodyOf(m.payload)
    let complete = body != null
    if (body == null) body = m.snippet ?? ''
    if (body.length > MAX_BODY) {
      body = body.slice(0, MAX_BODY)
      complete = false
    }
    return {
      emailId: m.id,
      from,
      to: parseAddresses(h.to ?? ''),
      cc: parseAddresses(h.cc ?? ''),
      date: m.internalDate ? new Date(Number(m.internalDate)).toISOString() : isoDate(h.date),
      incoming: !sent,
      bodyText: body,
      bodyComplete: complete,
    }
  })
  const first = t.messages?.[0]
  return { mode: 'server', threadId: t.id, subject: first ? headerMap(first).subject ?? '' : '', gmailUrl: gmailUrl(t.id), messages }
}

function headerMap(m: ApiMessage): Record<string, string> {
  const out: Record<string, string> = {}
  for (const h of m.payload?.headers ?? []) out[h.name.toLowerCase()] = h.value
  return out
}

/** The text of a message: its text/plain part, else its text/html part as text; null when it has neither. */
function bodyOf(part: ApiPart | undefined): string | null {
  if (!part) return null
  const plain = findPart(part, 'text/plain')
  if (plain?.body?.data) return b64urlDecode(plain.body.data)
  const html = findPart(part, 'text/html')
  if (html?.body?.data) return htmlToText(b64urlDecode(html.body.data))
  return null
}

function findPart(part: ApiPart, mimeType: string): ApiPart | null {
  if (part.mimeType === mimeType && part.body?.data) return part
  for (const p of part.parts ?? []) {
    const hit = findPart(p, mimeType)
    if (hit) return hit
  }
  return null
}

function htmlToText(html: string): string {
  const doc = new DOMParser().parseFromString(html.replace(/<br\s*\/?>/gi, '\n').replace(/<\/(p|div|li|tr)>/gi, '\n'), 'text/html')
  return (doc.body?.textContent ?? '').replace(/\n{3,}/g, '\n\n').trim()
}

/** Who a reply goes to, worked out as Gmail's Reply / Reply all would. */
export function replyRecipients(kind: DraftKind, h: Record<string, string>, viewerEmail: string): { to: Person[]; cc: Person[] } {
  const me = viewerEmail.toLowerCase()
  const notMe = (p: Person) => p.email.toLowerCase() !== me
  const from = parseAddresses(h['reply-to'] || h.from || '')
  const to = parseAddresses(h.to ?? '')
  const cc = parseAddresses(h.cc ?? '')
  const mine = parseAddresses(h.from ?? '').some((p) => !notMe(p))
  // Replying to my own message goes to whom I sent it to.
  const primary = mine ? to : from
  if (kind === 'reply') return { to: dedupe(primary), cc: [] }
  const rest = mine ? cc : [...to, ...cc]
  const toList = dedupe(primary)
  const seen = new Set(toList.map((p) => p.email.toLowerCase()))
  return { to: toList, cc: dedupe(rest.filter(notMe)).filter((p) => !seen.has(p.email.toLowerCase())) }
}

function dedupe(list: Person[]): Person[] {
  const seen = new Set<string>()
  return list.filter((p) => {
    const k = p.email.toLowerCase()
    if (seen.has(k)) return false
    seen.add(k)
    return true
  })
}

/** An address header (`"Doe, Jo" <jo@x.org>, ann@y.org`) as people; quoted commas stay in names. */
export function parseAddresses(header: string): Person[] {
  const parts: string[] = []
  let cur = '', quoted = false, angle = false
  for (const ch of header) {
    if (ch === '"') quoted = !quoted
    else if (ch === '<' && !quoted) angle = true
    else if (ch === '>' && !quoted) angle = false
    if (ch === ',' && !quoted && !angle) {
      parts.push(cur)
      cur = ''
    } else cur += ch
  }
  parts.push(cur)
  const out: Person[] = []
  for (const raw of parts) {
    const s = raw.trim()
    if (!s) continue
    const m = s.match(/^(.*)<([^>]+)>\s*$/)
    const email = (m ? m[2] : s).trim()
    if (!/^[^\s@]+@[^\s@]+$/.test(email)) continue
    const name = m ? m[1].trim().replace(/^"(.*)"$/, '$1').replace(/\\(.)/g, '$1') : ''
    out.push({ name, email })
  }
  return out
}

/** An RFC 5322 message, base64url as Gmail's `raw` takes it. Headers are encoded; the body is UTF-8 text, and
 *  with `html` a multipart/alternative of the text and the HTML (what chrome-sales's drafts were). */
export function mime(m: { to: Person[]; cc: Person[]; bcc: Person[]; subject: string; body: string; html?: string | null; headers: Record<string, string> }): string {
  const addr = (list: Person[]) => list.map((p) => (p.name ? `${encodeWord(p.name)} <${p.email}>` : p.email)).join(', ')
  const lines = [
    ...(m.to.length ? [`To: ${addr(m.to)}`] : []),
    ...(m.cc.length ? [`Cc: ${addr(m.cc)}`] : []),
    ...(m.bcc.length ? [`Bcc: ${addr(m.bcc)}`] : []),
    `Subject: ${encodeWord(m.subject)}`,
    ...Object.entries(m.headers).map(([k, v]) => `${k}: ${v.replace(/[\r\n]+/g, ' ')}`),
    'MIME-Version: 1.0',
    ...(m.html == null ? part('text/plain', m.body) : alternative(m.body, m.html)),
  ]
  return b64(utf8(lines.join('\r\n'))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function part(type: 'text/plain' | 'text/html', body: string): string[] {
  return [`Content-Type: ${type}; charset="UTF-8"`, 'Content-Transfer-Encoding: base64', '', wrap(b64(utf8(body.replace(/\r?\n/g, '\r\n'))))]
}

function alternative(text: string, html: string): string[] {
  const boundary = `canopy-${Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, '0')).join('')}`
  return [
    `Content-Type: multipart/alternative; boundary="${boundary}"`, '',
    `--${boundary}`, ...part('text/plain', text), '',
    `--${boundary}`, ...part('text/html', html), '',
    `--${boundary}--`,
  ]
}

function encodeWord(s: string): string {
  const clean = s.replace(/[\r\n]+/g, ' ')
  if (/^[\x20-\x7e]*$/.test(clean) && !/[",<>]/.test(clean)) return clean
  if (/^[\x20-\x7e]*$/.test(clean)) return `"${clean.replace(/(["\\])/g, '\\$1')}"`
  return `=?UTF-8?B?${b64(utf8(clean))}?=`
}

const utf8 = (s: string) => new TextEncoder().encode(s)
function b64(bytes: Uint8Array): string {
  let bin = ''
  for (const b of bytes) bin += String.fromCharCode(b)
  return btoa(bin)
}
const wrap = (s: string) => s.replace(/.{1,76}/g, '$&\r\n').trimEnd()
function b64urlDecode(data: string): string {
  const bin = atob(data.replace(/-/g, '+').replace(/_/g, '/'))
  return new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)))
}

// ---- shared --------------------------------------------------------------------------------------

function draftArgs(a: Record<string, unknown>) {
  const kind: DraftKind = a.kind === 'reply' || a.kind === 'reply_all' ? a.kind : a.kind == null || a.kind === 'new' ? 'new' : (() => {
    throw new OpError('bad_request', 'kind is "new", "reply" or "reply_all".')
  })()
  const subject = a.subject == null ? null : String(a.subject)
  const bodyHtml = a.bodyHtml == null ? null : String(a.bodyHtml)
  if (bodyHtml != null && bodyHtml.length > MAX_HTML) throw new OpError('bad_request', `bodyHtml is at most ${MAX_HTML.toLocaleString('en')} characters.`)
  if (subject != null && (subject.length > 1000 || /[\r\n]/.test(subject))) throw new OpError('bad_request', 'subject is one line of at most 1,000 characters.')
  return {
    kind, subject,
    to: people(a.to, 'to'), cc: people(a.cc, 'cc'), bcc: people(a.bcc, 'bcc'),
    bodyHtml,
    bodyText: a.bodyText != null ? text(a.bodyText, 'bodyText') : bodyHtml != null ? htmlToText(bodyHtml).slice(0, MAX_BODY) : '',
    threadId: a.threadId == null ? '' : String(a.threadId),
  }
}

function people(v: unknown, field: string): Person[] {
  if (v == null) return []
  if (!Array.isArray(v) || v.length > MAX_RECIPIENTS) throw new OpError('bad_request', `${field} is a list of at most ${MAX_RECIPIENTS} {email, name?}.`)
  return v.map((p) => {
    const email = typeof p === 'string' ? p : String((p as Person)?.email ?? '')
    if (!/^[^\s@,<>]+@[^\s@,<>]+$/.test(email)) throw new OpError('bad_request', `${field} has an address that isn't one: "${email}".`)
    return { name: typeof p === 'string' ? '' : String((p as Person).name ?? ''), email }
  })
}

function text(v: unknown, field: string): string {
  if (typeof v !== 'string' || v.length > MAX_BODY) throw new OpError('bad_request', `${field} is text of at most ${MAX_BODY.toLocaleString('en')} characters.`)
  return v
}

const person = (p: LocalPerson): Person => ({ name: p.name ?? '', email: String(p.email ?? '') })

/** A Gmail API id (lowercase hex). gmail.js's `thread-f:<decimal>` / `msg-f:<decimal>` convert exactly. */
export function hexId(id: string): string {
  const m = /^(?:thread|msg)-[fa]:(\d+)$/.exec(id)
  return m ? BigInt(m[1]).toString(16) : id.toLowerCase()
}

function gmailId(v: unknown): string {
  const id = hexId(String(v ?? ''))
  if (!/^[0-9a-f]{8,32}$/.test(id)) throw new OpError('bad_request', 'threadId is a Gmail thread id.')
  return id
}

const gmailUrl = (threadId: string) => `https://mail.google.com/mail/#all/${threadId}`

function isoDate(v: string | undefined): string {
  const t = v ? Date.parse(v) : NaN
  return Number.isNaN(t) ? (v ?? '') : new Date(t).toISOString()
}
