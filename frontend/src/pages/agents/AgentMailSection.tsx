import { useEffect, useState } from 'react'
import { Link, useOutletContext, useParams } from 'react-router-dom'
import { WorkbenchSkeleton, WorkbenchSubHeader } from 'canopy-ui'
import {
  fetchMailThread,
  fetchMailThreads,
  type MailMessageOut,
  type MailThreadOut,
} from '@/api/mailThreads'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'

// Delivery states of an outbound message, in semantic tokens.
const STATUS_CLASS: Record<string, string> = {
  sent: 'text-muted-foreground',
  delivered: 'text-success',
  bounced: 'text-destructive',
  complained: 'text-warning',
}

function MessageView({ m }: { m: MailMessageOut }) {
  return (
    <article className="border-b border-border py-3 last:border-b-0">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[12px] text-muted-foreground">
        <span className="rounded border border-border px-1.5 text-[11px] uppercase">{m.direction}</span>
        <span className="text-foreground">{m.from_name ? `${m.from_name} <${m.from_addr}>` : m.from_addr}</span>
        <span>to {m.to.join(', ')}</span>
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
          srcDoc={m.html}
          className="mt-2 h-72 w-full rounded border border-border bg-background"
        />
      ) : (
        <p className="mt-2 whitespace-pre-wrap text-[13px] text-foreground">{m.text}</p>
      )}
      {m.attachments.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-3 text-[12px]">
          {m.attachments.map((a) => (
            <li key={a.url}>
              <a href={a.url} download={a.filename} className="text-primary underline" rel="noopener noreferrer">
                {a.filename}
              </a>
            </li>
          ))}
        </ul>
      )}
    </article>
  )
}

export function AgentMailSection() {
  const { agent } = useOutletContext<AgentOutletContext>()
  const { workspace = '' } = useParams()
  const [threads, setThreads] = useState<MailThreadOut[] | null>(null)
  const [agentAddress, setAgentAddress] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [openId, setOpenId] = useState<string | null>(null)
  const [thread, setThread] = useState<MailThreadOut | null>(null)

  useEffect(() => {
    let alive = true
    setThreads(null)
    setError(null)
    fetchMailThreads(workspace, agent.slug)
      .then((r) => {
        if (!alive) return
        setThreads(r.items)
        setAgentAddress(r.agent_address)
      })
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : 'Failed to load mail'))
    return () => {
      alive = false
    }
  }, [workspace, agent.slug])

  useEffect(() => {
    if (!openId) return
    let alive = true
    setThread(null)
    fetchMailThread(workspace, openId)
      .then((t) => alive && setThread(t))
      .catch(() => alive && setThread(null))
    return () => {
      alive = false
    }
  }, [workspace, openId])

  return (
    <div className="px-6 py-8">
      <WorkbenchSubHeader title="Mail" count={threads?.length} />
      {error ? (
        <p role="alert" className="text-[13px] text-destructive">{error}</p>
      ) : threads === null ? (
        <WorkbenchSkeleton />
      ) : threads.length === 0 && agentAddress ? (
        <p className="text-[13px] text-muted-foreground">No mail yet for {agentAddress}.</p>
      ) : threads.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          This agent has no email address — an admin can add one in{' '}
          <Link to={scopedPath(workspace, '/settings/email')} className="text-primary underline">
            Settings → Email
          </Link>
          .
        </p>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
          <table className="w-full text-left text-[13px]">
            <thead className="text-[11px] uppercase text-muted-foreground">
              <tr>
                <th className="py-1 pr-2 font-medium">Subject</th>
                <th className="py-1 pr-2 font-medium">With</th>
                <th className="py-1 pr-2 font-medium">Msgs</th>
                <th className="py-1 font-medium">Last</th>
              </tr>
            </thead>
            <tbody>
              {threads.map((t) => (
                <tr
                  key={t.thread_id}
                  onClick={() => setOpenId(t.thread_id)}
                  aria-selected={t.thread_id === openId}
                  className={`cursor-pointer border-t border-border hover:bg-muted ${
                    t.thread_id === openId ? 'bg-muted' : ''
                  }`}
                >
                  <td className="py-1.5 pr-2">
                    {t.unread && <span aria-label="unread" className="mr-1.5 inline-block h-2 w-2 rounded-full bg-primary" />}
                    <span className={t.unread ? 'font-semibold text-foreground' : 'text-foreground'}>{t.subject}</span>
                  </td>
                  <td className="py-1.5 pr-2 text-muted-foreground">{t.counterpart || t.address}</td>
                  <td className="py-1.5 pr-2 text-muted-foreground">{t.message_count}</td>
                  <td className="py-1.5 text-muted-foreground">{relativeAge(t.last_message_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <section aria-label="Thread" className="min-w-0">
            {!openId ? (
              <p className="text-[13px] text-muted-foreground">Select a thread to read it.</p>
            ) : thread === null ? (
              <WorkbenchSkeleton />
            ) : (
              <>
                <h2 className="text-[14px] font-semibold text-foreground">{thread.subject}</h2>
                {thread.messages.map((m) => (
                  <MessageView key={m.id} m={m} />
                ))}
              </>
            )}
          </section>
        </div>
      )}
    </div>
  )
}
