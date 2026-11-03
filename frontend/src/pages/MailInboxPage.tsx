import { useEffect, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'
import { WorkbenchSkeleton, WorkbenchSubHeader } from 'canopy-ui'

import { fetchMailThread, fetchMailThreads, setMailThreadState, type MailThreadOut } from '@/api/mail'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'
import { MessageView } from '@/pages/agents/AgentMailSection'

// Your own inbox of mail canopy forwards to you for addresses an admin set to "keep a copy"
// (Settings → Email). Only you can read it: not admins, not other members. It is a reader,
// not a mail client; reply from your normal mailbox.

export function MailInboxPage(): JSX.Element {
  const { workspace = '' } = useParams()
  const [threads, setThreads] = useState<MailThreadOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [openId, setOpenId] = useState<string | null>(null)
  const [thread, setThread] = useState<MailThreadOut | null>(null)
  const [threadError, setThreadError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    fetchMailThreads(workspace, undefined, true)
      .then((r) => alive && setThreads(r.items))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : 'Failed to load mail'))
    return () => { alive = false }
  }, [workspace])

  useEffect(() => {
    if (!openId) return
    let alive = true
    setThread(null)
    setThreadError(null)
    fetchMailThread(workspace, openId)
      .then(async (t) => {
        if (!alive) return
        setThread(t)
        if (t.unread) {
          await setMailThreadState(workspace, openId, { unread: false }).catch(() => undefined)
          if (alive) setThreads((cur) => cur?.map((x) => (x.thread_id === openId ? { ...x, unread: false } : x)) ?? cur)
        }
      })
      .catch((e: unknown) => alive && setThreadError(e instanceof Error ? e.message : 'Failed to load thread'))
    return () => { alive = false }
  }, [workspace, openId])

  return (
    <div className="px-6 py-8">
      <WorkbenchSubHeader title="Forwarded mail" count={threads?.length} />
      <p className="mb-4 text-[12px] text-muted-foreground">
        Copies of mail sent to addresses that forward to you, kept here so one lost in transit can still be read.
        Only you can see this.
      </p>
      {error ? (
        <p role="alert" className="text-[13px] text-destructive">{error}</p>
      ) : threads === null ? (
        <WorkbenchSkeleton />
      ) : threads.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          Nothing yet. An admin turns this on per address with “Keep a copy in their canopy inbox” in{' '}
          <Link to={scopedPath(workspace, '/settings/email')} className="text-primary underline">Settings → Email</Link>.
          Only mail that arrives after that is kept.
        </p>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
          <table className="w-full text-left text-[13px]">
            <thead className="text-[11px] uppercase text-muted-foreground">
              <tr>
                <th className="py-1 pr-2 font-medium">Subject</th>
                <th className="py-1 pr-2 font-medium">From</th>
                <th className="py-1 pr-2 font-medium">To</th>
                <th className="py-1 font-medium">Last</th>
              </tr>
            </thead>
            <tbody>
              {threads.map((t) => (
                <tr key={t.thread_id} onClick={() => setOpenId(t.thread_id)}
                  aria-current={t.thread_id === openId ? 'true' : undefined}
                  className={`cursor-pointer border-t border-border hover:bg-muted ${t.thread_id === openId ? 'bg-muted' : ''}`}>
                  <td className="py-1.5 pr-2">
                    {t.unread && <span aria-label="unread" className="mr-1.5 inline-block h-2 w-2 rounded-full bg-primary" />}
                    <button type="button" onClick={(e) => { e.stopPropagation(); setOpenId(t.thread_id) }}
                      className={`text-left ${t.unread ? 'font-semibold text-foreground' : 'text-foreground'}`}>
                      {t.subject || '(no subject)'}
                    </button>
                  </td>
                  <td className="py-1.5 pr-2 text-muted-foreground">{t.counterpart}</td>
                  <td className="py-1.5 pr-2 text-muted-foreground">{t.address}</td>
                  <td className="py-1.5 text-muted-foreground">{relativeAge(t.last_message_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <section aria-label="Thread" className="min-w-0">
            {!openId ? (
              <p className="text-[13px] text-muted-foreground">Select a message to read it.</p>
            ) : threadError ? (
              <p role="alert" className="text-[13px] text-destructive">{threadError}</p>
            ) : thread === null ? (
              <WorkbenchSkeleton />
            ) : (
              <>
                <h2 className="text-[14px] font-semibold text-foreground">{thread.subject}</h2>
                {thread.messages.map((m) => <MessageView key={m.id} m={m} />)}
              </>
            )}
          </section>
        </div>
      )}
    </div>
  )
}
