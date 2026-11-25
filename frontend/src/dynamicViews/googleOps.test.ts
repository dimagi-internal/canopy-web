// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { GOOGLE_OPS, OpError, hexId, mime, opsCatalogue, parseAddresses, replyRecipients, runGoogleOp, serverThread, type OpHost } from './googleOps'
import { ModeStore } from '@/ext/modeStore'

const b64url = (s: string) => btoa(String.fromCharCode(...new TextEncoder().encode(s))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const unb64url = (s: string) => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0)))

const THREAD = '18c0a1b2c3d4e5f6'
const apiThread = {
  id: THREAD,
  messages: [
    {
      id: '18c0a1b2c3d4e5f6', labelIds: ['INBOX'], internalDate: '1732492800000', snippet: 'hi',
      payload: {
        mimeType: 'multipart/alternative',
        headers: [
          { name: 'From', value: '"Doe, Jo" <jo@partner.org>' }, { name: 'To', value: 'me@dimagi.com, ann@partner.org' },
          { name: 'Cc', value: 'bo@partner.org' }, { name: 'Subject', value: 'Pilot' }, { name: 'Message-ID', value: '<a1@partner.org>' },
        ],
        parts: [{ mimeType: 'text/plain', body: { data: b64url('Hello — first') } }, { mimeType: 'text/html', body: { data: b64url('<p>x</p>') } }],
      },
    },
    {
      id: '18c0a1b2c3d4e5f7', labelIds: ['SENT'], internalDate: '1732579200000', snippet: 'thanks',
      payload: { mimeType: 'text/html', headers: [{ name: 'From', value: 'Me <me@dimagi.com>' }, { name: 'To', value: 'jo@partner.org' }],
        body: { data: b64url('<div>Thanks<br>Me</div>') } },
    },
  ],
}

function host(over: Partial<OpHost> = {}) {
  const local = { request: vi.fn<(c: string, a: Record<string, unknown>, clicked: boolean) => Promise<unknown>>(async () => null) }
  const server = {
    call: vi.fn<(r: Record<string, unknown>) => Promise<{ status: number; body?: unknown }>>(async () => ({ status: 200, body: apiThread })),
    begin: vi.fn(async () => ({})),
  }
  const h: OpHost = { mode: 'server', local, server, clicked: true, viewerEmail: 'me@dimagi.com', ...over }
  return { h, local, server }
}

describe('modes are explicit', () => {
  it('an operation the mode lacks is refused not_in_mode, and nothing runs in the other mode', async () => {
    const { h, local, server } = host({ mode: 'server' })
    await expect(runGoogleOp('gmail.fill_compose', { bodyText: 'x' }, h)).rejects.toMatchObject({ code: 'not_in_mode' })
    expect(local.request).not.toHaveBeenCalled()
    expect(server.call).not.toHaveBeenCalled()
  })

  it('local mode outside the panel is refused, never sent to the server', async () => {
    const { h, server } = host({ mode: 'local', local: undefined })
    await expect(runGoogleOp('gmail.thread', { threadId: THREAD }, h)).rejects.toMatchObject({ code: 'not_in_mode' })
    expect(server.call).not.toHaveBeenCalled()
  })

  it('a local thread not on screen is not_on_screen, not a server read', async () => {
    const { h, local, server } = host({ mode: 'local' })
    local.request.mockResolvedValueOnce({ threadId: 'ffff0000aaaa1111', messages: [] })
    await expect(runGoogleOp('gmail.thread', { threadId: THREAD }, h)).rejects.toMatchObject({ code: 'not_on_screen' })
    local.request.mockResolvedValueOnce(null)
    await expect(runGoogleOp('gmail.thread', {}, h)).rejects.toMatchObject({ code: 'not_on_screen' })
    expect(server.call).not.toHaveBeenCalled()
  })

  it('unknown operations are refused', async () => {
    await expect(runGoogleOp('gmail.send', {}, host().h)).rejects.toBeInstanceOf(OpError)
  })

  it('the catalogue the page sees matches the registry', () => {
    expect(Object.keys(opsCatalogue())).toEqual(Object.keys(GOOGLE_OPS))
    for (const spec of Object.values(GOOGLE_OPS)) {
      if (spec.modes.includes('local')) expect(spec.local).toBeTruthy()
      if (spec.modes.includes('server')) expect(spec.server?.length).toBeGreaterThan(0)
    }
  })
})

describe('gmail.thread answers one shape in both modes', () => {
  it('server: the API thread, decoded and shaped', async () => {
    const { h, server } = host()
    const t = await runGoogleOp('gmail.thread', { threadId: THREAD }, h) as ReturnType<typeof serverThread>
    expect(server.call).toHaveBeenCalledWith({ provider: 'google', method: 'GET', path: `gmail/threads/${THREAD}`, query: { format: 'full' } })
    expect(t.mode).toBe('server')
    expect(t.subject).toBe('Pilot')
    expect(t.messages[0]).toMatchObject({
      emailId: '18c0a1b2c3d4e5f6', from: { name: 'Doe, Jo', email: 'jo@partner.org' }, incoming: true,
      bodyText: 'Hello — first', bodyComplete: true, cc: [{ name: '', email: 'bo@partner.org' }],
    })
    expect(t.messages[0].to.map((p) => p.email)).toEqual(['me@dimagi.com', 'ann@partner.org'])
    expect(t.messages[1]).toMatchObject({ incoming: false, bodyText: 'Thanks\nMe' })
    expect(t.messages[1].date).toBe('2024-11-26T00:00:00.000Z')
  })

  it('local: the open thread, with gmail.js ids made API ids', async () => {
    const { h, local } = host({ mode: 'local' })
    const decimal = BigInt('0x' + THREAD).toString()
    local.request.mockResolvedValueOnce({
      threadId: `thread-f:${decimal}`, subject: 'Pilot', gmailUrl: null,
      messages: [{ emailId: 'abc12345', from: { name: 'Jo', email: 'jo@partner.org' }, to: [{ email: 'me@dimagi.com' }], cc: [],
        date: '2024-11-25T00:00:00Z', incoming: true, bodyText: 'Hello', bodyComplete: false, expanded: false }],
    })
    const t = await runGoogleOp('gmail.thread', { threadId: THREAD }, h) as ReturnType<typeof serverThread>
    expect(local.request).toHaveBeenCalledWith('read_open_thread', {}, false)
    expect(t).toMatchObject({ mode: 'local', threadId: THREAD, gmailUrl: `https://mail.google.com/mail/#all/${THREAD}` })
    expect(t.messages[0]).toEqual({
      emailId: 'abc12345', from: { name: 'Jo', email: 'jo@partner.org' }, to: [{ name: '', email: 'me@dimagi.com' }], cc: [],
      date: '2024-11-25T00:00:00.000Z', incoming: true, bodyText: 'Hello', bodyComplete: false,
    })
    // The same keys either way.
    const s = serverThread(apiThread, 'me@dimagi.com')
    expect(Object.keys(t).sort()).toEqual(Object.keys(s).sort())
    expect(Object.keys(t.messages[0]).sort()).toEqual(Object.keys(s.messages[0]).sort())
  })
})

describe('local-only operations', () => {
  it('answer what is on screen marked with their mode, and nothing open is not_on_screen', async () => {
    const { h, local } = host({ mode: 'local' })
    local.request.mockResolvedValueOnce({ threadId: THREAD, subject: 'Pilot' })
    await expect(runGoogleOp('gmail.open_email', {}, h)).resolves.toEqual({ threadId: THREAD, subject: 'Pilot', mode: 'local' })
    local.request.mockResolvedValueOnce({ to: [], cc: [], bcc: [] })
    await expect(runGoogleOp('gmail.compose_recipients', {}, h)).resolves.toMatchObject({ mode: 'local', to: [] })
    local.request.mockResolvedValueOnce({ filled: { subject: false, body: true } })
    await expect(runGoogleOp('gmail.fill_compose', { bodyText: 'x' }, h)).resolves.toEqual({ filled: { subject: false, body: true }, mode: 'local' })
    for (const op of ['gmail.open_email', 'gmail.compose_recipients', 'gmail.fill_compose']) {
      local.request.mockResolvedValueOnce(null)
      await expect(runGoogleOp(op, { bodyText: 'x' }, h)).rejects.toMatchObject({ code: 'not_on_screen' })
    }
  })
})

describe('gmail.draft', () => {
  it('local: start_compose on screen with the click, unsaved', async () => {
    const { h, local, server } = host({ mode: 'local' })
    local.request.mockResolvedValueOnce({ kind: 'new', filled: {} })
    const d = await runGoogleOp('gmail.draft', { to: [{ email: 'jo@partner.org' }], subject: 'Hi', bodyText: 'Body' }, h)
    expect(local.request).toHaveBeenCalledWith('start_compose', { kind: 'new', to: [{ name: '', email: 'jo@partner.org' }], subject: 'Hi', bodyText: 'Body' }, true)
    expect(d).toEqual({ mode: 'local', where: 'compose', format: 'text', kind: 'new', draftId: null, threadId: null })
    expect(server.call).not.toHaveBeenCalled()
  })

  it('local: a reply to a thread that is not on screen is refused before anything is filled', async () => {
    const { h, local } = host({ mode: 'local' })
    local.request.mockResolvedValueOnce({ threadId: 'ffff0000aaaa1111' })
    await expect(runGoogleOp('gmail.draft', { kind: 'reply', threadId: THREAD, bodyText: 'x' }, h)).rejects.toMatchObject({ code: 'not_on_screen' })
    expect(local.request).toHaveBeenCalledTimes(1)
  })

  it('server: a reply-all is a saved draft in the thread, addressed as Gmail would, with its headers', async () => {
    const { h, server } = host()
    server.call
      .mockResolvedValueOnce({ status: 200, body: apiThread })
      .mockResolvedValueOnce({ status: 200, body: { id: 'r123', message: { id: 'm1', threadId: THREAD } } })
    const d = await runGoogleOp('gmail.draft', { kind: 'reply_all', threadId: THREAD, bodyText: 'Sounds good' }, h)
    expect(server.begin).toHaveBeenCalledTimes(1)
    expect(server.call.mock.calls[0][0]).toMatchObject({ method: 'GET', path: `gmail/threads/${THREAD}`, query: { format: 'metadata' } })
    const post = server.call.mock.calls[1][0] as { path: string; body: { message: { raw: string; threadId: string } } }
    expect(post.path).toBe('gmail/drafts')
    expect(post.body.message.threadId).toBe(THREAD)
    const raw = unb64url(post.body.message.raw)
    // The latest message is mine (to jo), so Reply all goes back to jo with no one else.
    expect(raw).toContain('To: jo@partner.org\r\n')
    expect(raw).not.toContain('Cc:')
    expect(raw).toContain('Subject: Re: ')
    expect(d).toEqual({ mode: 'server', where: 'drafts', format: 'text', kind: 'reply_all', draftId: 'r123', threadId: THREAD })
  })

  it('server: a write with no click is the lease\'s refusal, and nothing is posted', async () => {
    const { h, server } = host()
    server.begin.mockRejectedValueOnce(new Error('This needs a click.'))
    // The same code the browser's refusal of an act carries, so a page tells "click first" apart in either mode.
    await expect(runGoogleOp('gmail.draft', { bodyText: 'x' }, h)).rejects.toMatchObject({ code: 'needs_click', message: 'This needs a click.' })
    expect(server.call).not.toHaveBeenCalled()
  })

  it('bodyHtml: the server saves HTML with a text alternative; the compose on screen gets the text', async () => {
    const html = '<p>Hello <b>Jo</b></p><p>Best,<br>Me</p>'
    const srv = host()
    srv.server.call.mockResolvedValueOnce({ status: 200, body: { id: 'r9', message: { threadId: THREAD } } })
    const d = await runGoogleOp('gmail.draft', { to: [{ email: 'jo@partner.org' }], subject: 'Hi', bodyHtml: html }, srv.h)
    expect(d).toMatchObject({ mode: 'server', format: 'html', draftId: 'r9' })
    const raw = unb64url((srv.server.call.mock.calls[0][0] as { body: { message: { raw: string } } }).body.message.raw)
    expect(raw).toMatch(/Content-Type: multipart\/alternative; boundary="canopy-[0-9a-f]+"/)
    const parts = raw.split(/--canopy-[0-9a-f]+/)
    const decode = (p: string) => new TextDecoder().decode(Uint8Array.from(atob(p.split('\r\n\r\n')[1].replace(/\r\n/g, '')), (c) => c.charCodeAt(0)))
    expect(decode(parts.find((p) => p.includes('text/html'))!)).toBe(html)
    expect(decode(parts.find((p) => p.includes('text/plain'))!).replace(/\r\n/g, '\n')).toBe('Hello Jo\nBest,\nMe')
    const loc = host({ mode: 'local' })
    loc.local.request.mockResolvedValueOnce({ kind: 'new', filled: {} })
    expect(await runGoogleOp('gmail.draft', { bodyHtml: html }, loc.h)).toMatchObject({ mode: 'local', format: 'text' })
    expect(loc.local.request.mock.calls[0][1]).toMatchObject({ bodyText: 'Hello Jo\nBest,\nMe' })
  })

  it('refuses bad recipients the same way in both modes', async () => {
    for (const mode of ['local', 'server'] as const) {
      await expect(runGoogleOp('gmail.draft', { to: ['not an address'], bodyText: 'x' }, host({ mode }).h)).rejects.toMatchObject({ code: 'bad_request' })
    }
  })
})

describe('helpers', () => {
  it('reply recipients follow Gmail: reply-to wins, me is dropped, cc deduped', () => {
    const h = { from: 'Jo <jo@p.org>', 'reply-to': 'team@p.org', to: 'me@dimagi.com, ann@p.org', cc: 'Jo <jo@p.org>, bo@p.org' }
    expect(replyRecipients('reply', h, 'ME@dimagi.com')).toEqual({ to: [{ name: '', email: 'team@p.org' }], cc: [] })
    const all = replyRecipients('reply_all', h, 'me@dimagi.com')
    expect(all.to.map((p) => p.email)).toEqual(['team@p.org'])
    expect(all.cc.map((p) => p.email)).toEqual(['ann@p.org', 'jo@p.org', 'bo@p.org'])
  })

  it('addresses keep quoted commas in names and drop what is not an address', () => {
    expect(parseAddresses('"Doe, Jo" <jo@x.org>, ann@y.org, undisclosed-recipients:;')).toEqual([
      { name: 'Doe, Jo', email: 'jo@x.org' }, { name: '', email: 'ann@y.org' },
    ])
  })

  it('a MIME draft is UTF-8, encodes non-ASCII headers and keeps header injection out', () => {
    const raw = unb64url(mime({ to: [{ name: 'Zoë', email: 'z@x.org' }], cc: [], bcc: [], subject: 'Hi\r\nBcc: evil@x.org', body: 'Ça va?', headers: {} }))
    expect(raw).toContain('To: =?UTF-8?B?')
    expect(raw).toContain('Subject: Hi Bcc: evil@x.org\r\n')
    expect(raw.split('\r\n').filter((l) => l.startsWith('Bcc:'))).toEqual([])
    const body = raw.split('\r\n\r\n')[1].replace(/\r\n/g, '')
    expect(new TextDecoder().decode(Uint8Array.from(atob(body), (c) => c.charCodeAt(0)))).toBe('Ça va?')
  })

  it('gmail.js ids convert exactly', () => {
    expect(hexId('thread-f:1789012345678901234')).toBe(BigInt('1789012345678901234').toString(16))
    expect(hexId('18C0A1')).toBe('18c0a1')
  })
})

describe('ModeStore', () => {
  it('is server until the person switches, keeps the choice, and tells listeners', () => {
    const kept = new Map<string, string>()
    const storage = { getItem: (k: string) => kept.get(k) ?? null, setItem: (k: string, v: string) => void kept.set(k, v) }
    const a = new ModeStore('canopy.mode:dimagi/sales', storage)
    expect(a.get()).toBe('server')
    const heard: string[] = []
    a.subscribe((m) => heard.push(m))
    a.set('local')
    expect(heard).toEqual(['local'])
    expect(new ModeStore('canopy.mode:dimagi/sales', storage).get()).toBe('local')
    expect(() => a.set('cloud' as never)).toThrow()
  })
})
