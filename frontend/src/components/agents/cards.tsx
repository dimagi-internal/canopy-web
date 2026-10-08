// Shared presentational pieces for the Agent Workspace sections. Extracted
// verbatim from the old single-column AgentWorkspacePage so the lazy-loaded
// section routes (projects / tasks / turns / skills …) can share them.
// Styling is preserved exactly as it was inline on the page.

import type {
  AgentSkillOut,
  AgentSyncOut,
  AgentTurnOut,
} from '@/api/agents'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Markdown } from '@/components/Markdown'
import { EventLedger } from '@/components/activity/EventLedger'
import { TurnTranscript } from '@/components/activity/TurnTranscript'
import { statusToken } from '@/components/activity/turnLog'
import { promptHasMore, turnBody, turnDuration, turnHeadline, turnTrigger } from './turnText'

export function formatDate(s: string): string {
  return new Date(s).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

export function formatPeriod(start: string, end: string): string {
  const s = formatDate(start)
  const e = formatDate(end)
  return s === e ? s : `${s} – ${e}`
}

export function CountStat({ value, label }: { value: number; label: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-lg font-semibold text-foreground leading-none">{value}</span>
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground mt-1">{label}</span>
    </div>
  )
}

export function SectionHeading({ label, count }: { label: string; count?: number }) {
  return (
    <div className="flex items-baseline gap-2 mb-3 mt-8 first:mt-0">
      <h2 className="text-[11px] font-bold uppercase tracking-[0.08em] text-primary">{label}</h2>
      {count !== undefined && <span className="text-[11px] text-muted-foreground">{count}</span>}
    </div>
  )
}

// "work: C+" → an outlined badge. Generic so any grade dimension renders.
function GradeBadge({ dimension, grade }: { dimension: string; grade: string }) {
  return (
    <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-foreground bg-muted border border-border px-2 py-0.5 rounded">
      <span className="text-muted-foreground">{dimension}:</span>
      <span className="text-primary">{grade}</span>
    </span>
  )
}

function OpenDocChip({ url, label }: { url: string; label: string }) {
  if (!url) return null
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 text-[11px] font-medium text-muted-foreground hover:text-primary bg-muted border border-border hover:border-primary/50 px-2.5 py-1 rounded-md transition-colors"
    >
      <span className="text-primary/70">↗</span>
      {label}
    </a>
  )
}

export function SyncCard({ sync }: { sync: AgentSyncOut }) {
  const grades = Object.entries(sync.self_grades ?? {})
  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[11px] text-muted-foreground">{formatPeriod(sync.period_start, sync.period_end)}</p>
          <h3 className="text-[15px] font-semibold text-foreground mt-0.5 leading-snug">{sync.title}</h3>
        </div>
        <OpenDocChip url={sync.doc_url} label="Open in Google Docs" />
      </div>
      {sync.summary && (
        <Markdown className="text-[13px] text-muted-foreground leading-relaxed mt-2">
          {sync.summary}
        </Markdown>
      )}
      {grades.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mt-3">
          {grades.map(([dimension, grade]) => (
            <GradeBadge key={dimension} dimension={dimension} grade={grade} />
          ))}
        </div>
      )}
    </div>
  )
}

// A short, safe label for a deliverable url (host + last path segment).
function urlLabel(url: string): string {
  try {
    const u = new URL(url)
    const last = u.pathname.split('/').filter(Boolean).pop()
    return last ? `${u.hostname}/…/${last}` : u.hostname
  } catch {
    return url
  }
}

function formatDateTime(s: string): string {
  return new Date(s).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

// A turn's own record: what it was asked (prompt), how it ended (status,
// result note), and — when the agent closed it out — what it reported. The
// header is a button: open, it shows the full prompt and the turn's event
// ledger. Links (transcript, deliverables) sit outside it so they stay links.
export function TurnCard({ turn }: { turn: AgentTurnOut }) {
  const [open, setOpen] = useState(false)
  // The transcript is optional — only render its link when it was uploaded. The
  // server builds it under the workspace it was shared from (canopy-web#1337).
  const shareHref = turn.share_url ?? ''
  // Where the turn's actual work lives: the chat it ran in (a laptop runner's
  // emdash session, or a chat turn's own session) — or, for a cloud-runner turn
  // that has no chat, its retained transcript, shown inline when opened.
  const { workspace } = useParams<{ workspace: string }>()
  const chatHref =
    turn.chat_session_id && workspace ? `/w/${workspace}/chat/${turn.chat_session_id}` : ''
  const showTranscript = !turn.chat_session_id && !!turn.has_transcript
  const body = turnBody(turn)
  const duration = turnDuration(turn)
  return (
    <div id={turn.id} className="scroll-mt-6 bg-card border border-border rounded-xl hover:border-primary/40 transition-colors">
      <div className="flex items-start justify-between gap-3 p-5 pb-0">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="min-w-0 flex-1 text-left cursor-pointer"
        >
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
            <span title={new Date(turn.created_at).toLocaleString()}>{formatDateTime(turn.created_at)}</span>
            {turn.status && (
              <span className={`rounded border px-1.5 py-px text-[10px] ${statusToken(turn.status)}`}>
                {turn.status}
              </span>
            )}
            <span>{turnTrigger(turn)}</span>
            {duration && <span>· {duration}</span>}
            {!turn.reported_at && (
              <span title="The agent did not file a close-out report for this turn">· no report</span>
            )}
          </div>
          <h3 className="text-[15px] font-semibold text-foreground mt-1 leading-snug">
            <span className="text-muted-foreground mr-1.5" aria-hidden>{open ? '▾' : '▸'}</span>
            {turnHeadline(turn)}
          </h3>
        </button>
        <div className="flex shrink-0 flex-wrap justify-end gap-1.5">
          {chatHref && (
            <Link
              to={chatHref}
              className="inline-flex items-center gap-1 text-[11px] font-medium text-muted-foreground hover:text-primary bg-muted border border-border hover:border-primary/50 px-2.5 py-1 rounded-md transition-colors"
            >
              <span className="text-primary/70">→</span>
              Open chat
            </Link>
          )}
          {shareHref && <OpenDocChip url={shareHref} label="View transcript" />}
        </div>
      </div>
      <div className="px-5 pb-5">
        {body && (
          <Markdown
            className={`text-[13px] text-muted-foreground leading-relaxed mt-2 ${open ? '' : 'line-clamp-3'}`}
          >
            {body}
          </Markdown>
        )}
        {(turn.task_ext_ids ?? []).length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 mt-3">
            <span className="text-[10px] uppercase tracking-wide text-muted-foreground">Advanced</span>
            {(turn.task_ext_ids ?? []).map((id) => (
              <span
                key={id}
                className="text-[10px] font-semibold text-primary bg-muted border border-border px-1.5 py-0.5 rounded"
              >
                {id}
              </span>
            ))}
          </div>
        )}
        {(turn.work_product_urls ?? []).length > 0 && (
          <div className="flex flex-col gap-1 mt-3">
            {(turn.work_product_urls ?? []).map((url) => (
              <a
                key={url}
                href={url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-primary transition-colors w-fit"
              >
                <span className="text-primary/70">↗</span>
                {urlLabel(url)}
              </a>
            ))}
          </div>
        )}
        {open && (
          <div className="mt-4 space-y-3 border-t border-border pt-3">
            {promptHasMore(turn) && (
              <div>
                <p className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">Prompt</p>
                <pre className="whitespace-pre-wrap break-words text-[12px] text-foreground-secondary font-sans max-h-80 overflow-y-auto">
                  {turn.prompt}
                </pre>
              </div>
            )}
            {turn.session_key && (
              <p className="text-[11px] text-muted-foreground">
                Session <code className="text-foreground-secondary">{turn.session_key}</code>
              </p>
            )}
            {showTranscript && (
              <div>
                <p className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">Transcript</p>
                <TurnTranscript turnId={turn.id} />
              </div>
            )}
            <div>
              <p className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">Events</p>
              <EventLedger turnId={turn.id} />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

export function SkillCard({ skill }: { skill: AgentSkillOut }) {
  return (
    <div className="bg-card border border-border rounded-xl p-4">
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-[14px] font-semibold text-foreground leading-snug min-w-0">{skill.name}</h3>
        <OpenDocChip url={skill.url} label="SKILL.md" />
      </div>
      {skill.description && (
        <p className="text-[12px] text-muted-foreground leading-relaxed mt-2">{skill.description}</p>
      )}
      {skill.improvement_note && (
        <p className="text-[12px] text-foreground leading-relaxed mt-2 pl-3 border-l-2 border-primary/40">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-primary/80 mr-1">
            Improvement
          </span>
          {skill.improvement_note}
        </p>
      )}
    </div>
  )
}
