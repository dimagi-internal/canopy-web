import { toneText } from 'canopy-ui/lib'

import type { MailMessageOut } from '@/api/mail'
import { relativeAge } from '@/lib/relativeAge'

// The email's own HTML is untrusted. sandbox="" blocks scripts, but <img>, CSS
// url() and <link> would still fetch remotely (a tracking pixel reveals the
// viewer's IP and read time), so the CSP goes FIRST and allows no network.
const CSP_META =
  `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">`
// CSP does not stop navigation: a <meta http-equiv="refresh"> would send the
// frame to any URL. Parse the way the browser will (any case, any attribute
// order, odd spacing) and drop every refresh meta before the frame sees it.
export function stripMetaRefresh(html: string): string {
  const doc = new DOMParser().parseFromString(html, 'text/html')
  const refresh = Array.from(doc.querySelectorAll('meta[http-equiv]')).filter(
    (el) => (el.getAttribute('http-equiv') ?? '').trim().toLowerCase() === 'refresh',
  )
  if (refresh.length === 0) return html
  refresh.forEach((el) => el.remove())
  return doc.documentElement.outerHTML
}

export function sandboxedSrcDoc(html: string): string {
  return CSP_META + stripMetaRefresh(html)
}

// Delivery states of an outbound message, in semantic tokens.
const STATUS_CLASS: Record<string, string> = {
  sent: toneText('neutral'),
  delivered: toneText('success'),
  bounced: toneText('destructive'),
  complained: toneText('warning'),
}

export function MessageView({ m }: { m: MailMessageOut }) {
  return (
    <article className="border-b border-border py-3 last:border-b-0">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[12px] text-muted-foreground">
        <span className="rounded border border-border px-1.5 text-[11px] uppercase">{m.direction}</span>
        <span className="text-foreground">{m.from_name ? `${m.from_name} <${m.from_addr}>` : m.from_addr}</span>
        <span>to {m.to.join(', ')}</span>
        {m.cc.length > 0 && <span>cc {m.cc.join(', ')}</span>}
        <time dateTime={m.created_at}>{relativeAge(m.created_at)}</time>
        {m.direction === 'out' && m.delivery_status && (
          <span className={STATUS_CLASS[m.delivery_status] ?? 'text-muted-foreground'}>{m.delivery_status}</span>
        )}
      </header>
      {m.html ? (
        // Untrusted markup: an empty sandbox means no scripts, no same-origin,
        // no forms, no popups. Never dangerouslySetInnerHTML.
        <iframe
          title={`Message ${m.id}`}
          sandbox=""
          srcDoc={sandboxedSrcDoc(m.html)}
          referrerPolicy="no-referrer"
          className="mt-2 h-72 w-full rounded border border-border bg-background"
        />
      ) : (
        <p className="mt-2 whitespace-pre-wrap text-[13px] text-foreground">{m.text}</p>
      )}
      {m.attachments.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-3 text-[12px]">
          {m.attachments.map((a, i) => (
            <li key={`${i}-${a.filename}`}>
              {/^https:\/\//.test(a.url) ? (
                <a href={a.url} target="_blank" rel="noopener noreferrer" className="text-primary underline">
                  {a.filename}
                </a>
              ) : (
                <span className="text-muted-foreground">
                  {a.filename}
                  {a.kept === false && ' (too large, not kept)'}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </article>
  )
}

