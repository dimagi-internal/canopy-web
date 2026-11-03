import { useState } from 'react'
import { toneText } from 'canopy-ui/lib'

import type { MailMessageOut } from '@/api/mail'

// The email's own HTML is untrusted. sandbox blocks scripts, forms and same-origin, but <img>, CSS
// url() and <link> would still fetch remotely (a tracking pixel reveals the viewer's IP and read
// time), so the CSP goes FIRST and allows no network. "Show images" lifts that for images only, for
// one message, on the reader's own click.
const cspMeta = (images: boolean) =>
  `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${images ? 'https: data:' : 'data:'}; style-src 'unsafe-inline'">`
// Mail is written for a white page. Without this, a message that sets no colours is black text on
// the app's dark background. Its own styles come later, so they still win.
const PAGE =
  `<meta name="color-scheme" content="light"><meta name="referrer" content="no-referrer"><base target="_blank">` +
  `<style>html{background:#fff;color:#202124;color-scheme:light;font-family:system-ui,sans-serif;font-size:14px}</style>`

// CSP does not stop navigation: a <meta http-equiv="refresh"> would send the frame to any URL.
// Parse the way the browser will (any case, any attribute order, odd spacing) and drop every refresh
// meta before the frame sees it. Links open in a new tab, never inside the frame, and only to web
// and mailto addresses (the frame's popups escape the sandbox, so a script: URL must not get there).
export function prepareHtml(html: string): string {
  const doc = new DOMParser().parseFromString(html, 'text/html')
  const refresh = Array.from(doc.querySelectorAll('meta[http-equiv]')).filter(
    (el) => (el.getAttribute('http-equiv') ?? '').trim().toLowerCase() === 'refresh',
  )
  const bases = Array.from(doc.querySelectorAll('base'))
  const links = Array.from(doc.querySelectorAll('a[href], area[href]'))
  if (refresh.length === 0 && bases.length === 0 && links.length === 0) return html
  refresh.forEach((el) => el.remove())
  bases.forEach((el) => el.remove())
  links.forEach((el) => {
    if (/^\s*(https?:|mailto:)/i.test(el.getAttribute('href') ?? '')) {
      el.setAttribute('target', '_blank')
      el.setAttribute('rel', 'noopener noreferrer')
    } else {
      el.removeAttribute('href')
    }
  })
  return doc.documentElement.outerHTML
}

export function sandboxedSrcDoc(html: string, images = false): string {
  return cspMeta(images) + PAGE + prepareHtml(html)
}

export function hasRemoteImages(html: string): boolean {
  return /<img\b[^>]*\ssrc\s*=\s*["']?\s*https?:/i.test(html) || /url\(\s*["']?\s*https?:/i.test(html)
}

/** Why this inbound message's sender could not be trusted, from the checks SES ran on receipt. */
export function senderWarning(verdicts: Record<string, string> | undefined): string | null {
  const v = verdicts ?? {}
  if (v.dmarc === 'FAIL') return 'This message failed its domain\'s sender check (DMARC). The sender may not be who they say.'
  if (v.spf === 'FAIL' && v.dkim === 'FAIL') return 'This message failed its sender checks (SPF and DKIM). The sender may not be who they say.'
  return null
}

/** Today: the time. This year: the day. Older: with the year. The full moment goes in a `title`. */
export function mailDate(iso: string, now = new Date()): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  const sameYear = d.getFullYear() === now.getFullYear()
  return d.toLocaleDateString([], { month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }) })
}

export function fullDate(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString([], { dateStyle: 'full', timeStyle: 'short' })
}

export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Splits off the trailing quote of earlier mail: an "On … wrote:" line or a run of "> " lines to the end. */
export function splitQuoted(text: string): { said: string; quoted: string } {
  const lines = text.split('\n')
  const rest = (i: number) => lines.slice(i).every((l) => l.trim() === '' || l.startsWith('>'))
  for (let i = 0; i < lines.length; i++) {
    const intro = /^On .*wrote:\s*$/.test(lines[i].trim()) && rest(i + 1)
    if (intro || (lines[i].startsWith('>') && rest(i))) {
      return { said: lines.slice(0, i).join('\n').trimEnd(), quoted: lines.slice(i).join('\n') }
    }
  }
  return { said: text, quoted: '' }
}

const STATUS_CLASS: Record<string, string> = {
  sent: toneText('neutral'),
  delivered: toneText('success'),
  bounced: toneText('destructive'),
  rejected: toneText('destructive'),
  complained: toneText('warning'),
}

const linkButton = 'text-[12px] text-primary hover:underline'

/** One message of a thread. Collapsed, it is one line that opens on a click. */
export function MessageView({ m, initiallyOpen = true, inSpam = false }: {
  m: MailMessageOut
  initiallyOpen?: boolean
  inSpam?: boolean
}) {
  const [open, setOpen] = useState(initiallyOpen)
  const [showQuoted, setShowQuoted] = useState(false)
  const [images, setImages] = useState(false)
  const who = m.from_name || m.from_addr
  if (!open) {
    return (
      <article className="border-b border-border last:border-b-0">
        <button type="button" onClick={() => setOpen(true)} aria-expanded={false}
          className="flex w-full items-baseline gap-2 py-2 text-left text-[12px] hover:bg-muted">
          <span className="shrink-0 text-foreground">{who}</span>
          <span className="min-w-0 flex-1 truncate text-muted-foreground">{splitQuoted(m.text).said.replace(/\s+/g, ' ')}</span>
          <time className="shrink-0 text-muted-foreground" dateTime={m.created_at} title={fullDate(m.created_at)}>{mailDate(m.created_at)}</time>
        </button>
      </article>
    )
  }
  const warning = m.direction === 'in' ? senderWarning(m.verdicts as Record<string, string>) : null
  const { said, quoted } = splitQuoted(m.text)
  return (
    <article className="border-b border-border py-3 last:border-b-0">
      <header className="text-[12px] text-muted-foreground">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-medium text-foreground">{m.from_name || m.from_addr}</span>
          {m.from_name && <span>&lt;{m.from_addr}&gt;</span>}
          <time className="ml-auto" dateTime={m.created_at} title={fullDate(m.created_at)}>{mailDate(m.created_at)}</time>
        </div>
        <div className="flex flex-wrap gap-x-3">
          <span>to {m.to.join(', ')}</span>
          {m.cc.length > 0 && <span>cc {m.cc.join(', ')}</span>}
          {(m.bcc ?? []).length > 0 && <span>bcc {(m.bcc ?? []).join(', ')}</span>}
          {m.direction === 'out' && m.delivery_status && (
            <span className={STATUS_CLASS[m.delivery_status] ?? ''}>{m.delivery_status}</span>
          )}
        </div>
      </header>
      {warning && <p role="alert" className={`mt-2 rounded-md border border-border px-2 py-1 text-[12px] ${toneText('warning')}`}>{warning}</p>}
      {m.html ? (
        <>
          {!inSpam && !images && hasRemoteImages(m.html) && (
            <p className="mt-2 text-[12px] text-muted-foreground">
              Images are hidden, so the sender can&rsquo;t tell you opened this.{' '}
              <button type="button" className={linkButton} onClick={() => setImages(true)}>Show images</button>
            </p>
          )}
          {/* Untrusted markup: no scripts, no same-origin, no forms. Popups only so a link can open
              in a new tab. Never allow-same-origin, never dangerouslySetInnerHTML. */}
          <iframe
            title={`Message ${m.id}`}
            sandbox="allow-popups allow-popups-to-escape-sandbox"
            srcDoc={sandboxedSrcDoc(m.html, images)}
            referrerPolicy="no-referrer"
            className="mt-2 h-[70dvh] w-full rounded border border-border"
          />
        </>
      ) : (
        <div className="mt-2 text-[13px] text-foreground">
          <p className="whitespace-pre-wrap break-words">{said}</p>
          {quoted && (
            <>
              <button type="button" className={`${linkButton} mt-1`} aria-expanded={showQuoted} onClick={() => setShowQuoted((v) => !v)}>
                {showQuoted ? 'Hide quoted text' : 'Show quoted text'}
              </button>
              {showQuoted && <p className="mt-1 whitespace-pre-wrap break-words text-muted-foreground">{quoted}</p>}
            </>
          )}
        </div>
      )}
      {m.attachments.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-2 text-[12px]">
          {m.attachments.map((a, i) => (
            <li key={`${i}-${a.filename}`} className="rounded-md border border-border px-2 py-1">
              {/^https:\/\//.test(a.url) ? (
                <a href={a.url} target="_blank" rel="noopener noreferrer" className="text-primary underline">{a.filename}</a>
              ) : (
                <span className="text-muted-foreground">{a.filename}</span>
              )}
              <span className="ml-1 text-muted-foreground">
                {fileSize(a.size)}
                {a.kept === false && ', too large, not kept'}
                {inSpam && a.kept !== false && ', not offered in Spam'}
              </span>
            </li>
          ))}
        </ul>
      )}
    </article>
  )
}
