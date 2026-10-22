import { useEffect, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { WorkbenchSkeleton } from 'canopy-ui'
import { getHuddle, type Huddle } from '@/api/huddles'
import { getHuddleThreads } from '@/api/threads'
import { relativeAge } from '@/lib/relativeAge'
import { ProposalCard } from '@/components/projects/ProposalCard'
import { useEngagementView } from '@/components/projects/useEngagementView'
import { HuddleConversation } from './HuddleConversation'
import { readView } from './conversationModel'
import { MemberAvatar } from './MemberAvatar'
import { HuddleOutcome } from './HuddleOutcome'
import { PrioritiesBrief } from './HuddleBrief'
import { BriefContext } from './briefContext'
import { agreementThreads, columns, countdown, roundName, roundsToShow } from './huddleModel'
import { andList, huddleExplainer, who } from './plainWords'
import { scopedPath } from '../../lib/scopedLinks'

/**
 * One huddle, for a person seeing it cold: a one-line explainer of what a
 * huddle is, what was decided and what waits on the reader, then how it went —
 * the Story by default (three plain steps and the result), the Full
 * conversation (by agent / by idea, verbatim) or the Diagram. Every word a
 * reader sees comes from plainWords; the engine's own terms stay in the data.
 * Polls while the huddle is in flight; everything on it is derived
 * server-side from turns and tasks (apps/huddles).
 */

const POLL_MS = 15_000

function Pill({ className, children }: { className: string; children: React.ReactNode }) {
  return (
    <span className={`inline-flex h-5 items-center gap-1.5 rounded-full border px-2 text-[11px] font-medium ${className}`}>
      {children}
    </span>
  )
}

function Stepper({ huddle }: { huddle: Huddle }) {
  // Settling changes in direct conversations is step 4 too (the Diagram's).
  const threads = agreementThreads(huddle)
  const rounds = roundsToShow(huddle)
  if (threads.length && !rounds.includes(4)) rounds.push(4)
  const talking = threads.some((t) => t.status === 'open')
  return (
    <ol aria-label="Steps" className="flex flex-wrap items-center gap-2">
      {rounds.map((r, i) => {
        const cells = huddle.cells.filter((c) => c.round === r)
        const replied = cells.filter((c) => c.block).length
        const viaThreads = r === 4 && threads.length > 0
        const done = viaThreads ? !talking
          : huddle.finished || (cells.length > 0 && replied >= cells.length && r < huddle.rounds_dispatched)
        const current = viaThreads ? talking : !huddle.finished && r === huddle.rounds_dispatched
        return (
          <li key={r} className="flex items-center gap-2">
            {i > 0 && <span aria-hidden className={'h-px w-4 sm:w-6 ' + (r <= huddle.rounds_dispatched || viaThreads ? 'bg-primary/50' : 'bg-border')} />}
            <span
              className={
                'inline-flex items-center gap-2 rounded-full border px-2.5 py-1 text-[12px] ' +
                (current
                  ? 'border-primary/50 bg-primary/10 text-foreground'
                  : done
                    ? 'border-border bg-card text-foreground-secondary'
                    : 'border-dashed border-border text-muted-foreground')
              }
            >
              <span
                className={
                  'inline-flex size-5 items-center justify-center rounded-full text-[11px] font-semibold ' +
                  (current ? 'bg-primary text-primary-foreground' : done ? 'bg-success/20 text-success' : 'bg-muted text-muted-foreground')
                }
              >
                {done && !current ? '✓' : r}
              </span>
              {roundName(huddle.type, r)}
              {current && cells.length > 0 && (
                <span className="text-[11px] text-muted-foreground">{replied} of {cells.length} answered</span>
              )}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

/** "Weekly check-in on Canopy support · #channel · lead Y · read N messages (A → B)" —
 * set when the huddle ran for a project check-in's run, null otherwise. The
 * header links back to the project, where the check-in and its record live. */
function CheckinLine({ checkin }: { checkin: NonNullable<Huddle['checkin']> }) {
  const ts = (v: string) => {
    const n = Number(v)
    return v && Number.isFinite(n)
      ? new Date(n * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
      : v
  }
  const read = checkin.read
  const p = checkin.project
  return (
    <p data-checkin={checkin.name} className="mt-2 text-[13px] text-foreground-secondary">
      <Link className="font-medium text-primary hover:underline" to={`${p.url}#project-checkins`}>
        {checkin.name}
      </Link>{' '}
      on{' '}
      <Link className="font-medium text-primary hover:underline" to={p.url}>
        {p.name}
      </Link>
      {checkin.channel_name ? <> · #{checkin.channel_name}</> : <> · no channel</>}
      {checkin.lead_email && <> · lead {checkin.lead_email}</>}
      {read && (
        <>
          {' '}· read {read.messages} message{read.messages === 1 ? '' : 's'}
          {read.first_ts && read.last_ts && <> ({ts(read.first_ts)} → {ts(read.last_ts)})</>}
        </>
      )}
    </p>
  )
}

/** What the huddle proposed — tasks in its check-in's project, and (from huddles
 * before check-ins) projects — on the same card the project page shows, so
 * people back, comment and decide right here. */
function HuddleProposals({ huddle, onChanged }: { huddle: Huddle; onChanged: () => void }) {
  const projects = huddle.projects ?? []
  const tasks = huddle.tasks ?? []
  const open =
    projects.filter((p) => p.status === 'proposed').length + tasks.filter((t) => t.status === 'suggested').length
  const parts = [
    tasks.length ? `${tasks.length} proposed task${tasks.length === 1 ? '' : 's'}` : '',
    projects.length ? `${projects.length} proposed project${projects.length === 1 ? '' : 's'}` : '',
  ].filter(Boolean)
  return (
    <section aria-labelledby="huddle-projects" className="space-y-4">
      <div>
        <h2 id="huddle-projects" className="text-base font-semibold text-foreground">What it proposes</h2>
        <p className="mt-1 text-[14px] leading-relaxed text-foreground-secondary">
          {parts.join(' and ')}
          {open ? `, ${open} still waiting for a decision` : ''}. Back one, say what you would change, or ask —
          the agents read it before the next huddle.
        </p>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        {tasks.map((t) => (
          <ProposalCard key={`task-${t.agent}-${t.ref}`} task={t} onChanged={onChanged} linkToProject />
        ))}
        {projects.map((p) => (
          <ProposalCard key={`${p.agent}-${p.ref}`} proposal={p} onChanged={onChanged} linkToProject />
        ))}
      </div>
    </section>
  )
}

export function HuddlePage() {
  const { workspace = '', id = '' } = useParams()
  const [huddle, setHuddle] = useState<Huddle | null>(null)
  const [error, setError] = useState('')
  const [now, setNow] = useState(() => new Date())
  const [params] = useSearchParams()
  // Bumped after a person acts on a proposed project, to re-read the huddle now.
  const [version, setVersion] = useState(0)
  useEngagementView(id ? { huddle_id: id } : null)
  // The Story ends with the full result, so above it the summary stays short.
  const story = readView(params.get('view')) === 'story'

  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const load = () => {
      // The agreement threads hanging off the huddle ride along; a failure to load
      // them leaves the huddle readable as relayed (no direct conversations).
      Promise.all([getHuddle(id), getHuddleThreads(id).catch(() => undefined)])
        .then(([h, threads]) => {
          if (threads) h = { ...h, threads }
          if (!alive) return
          setHuddle(h)
          setError('')
          setNow(new Date())
          if (!h.finished || h.threads?.some((t) => t.status === 'open')) timer = setTimeout(load, POLL_MS)
        })
        .catch((e: unknown) => {
          if (!alive) return
          setError(e instanceof Error ? e.message : String(e))
          timer = setTimeout(load, POLL_MS * 2)
        })
    }
    load()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
  }, [id, version])

  if (!huddle) {
    return (
      <div className="mx-auto max-w-7xl p-6">
        <Link to={scopedPath(workspace, '/huddles')} className="text-[12px] text-muted-foreground hover:text-primary">← Huddles</Link>
        {error ? <p className="mt-4 text-sm text-destructive">Couldn’t load this huddle: {error}</p> : <div className="mt-4"><WorkbenchSkeleton /></div>}
      </div>
    )
  }

  const due = countdown(huddle.deadline_at, now)
  const when = huddle.created_at
    ? new Date(huddle.created_at).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
    : ''
  return (
    // pb-28: room under the last cards for the site-wide floating "Canopy AI"
    // button, which otherwise covers the bottom-right member card.
    <BriefContext.Provider value={huddle.priorities_brief ?? ''}>
      <div className="pb-28">
        <Link to={scopedPath(workspace, '/huddles')} className="text-[12px] text-muted-foreground hover:text-primary">← Huddles</Link>

        <header className="mt-3 mb-6 rounded-2xl border border-border bg-card p-5">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0 max-w-[640px]">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-xl font-semibold text-foreground">
                  {who(huddle.leader)}&apos;s huddle{when ? <span className="font-normal text-muted-foreground"> · {when}</span> : null}
                </h1>
                {huddle.finished ? (
                  <Pill className="border-success/30 bg-success/10 text-success">finished</Pill>
                ) : (
                  <Pill className="border-info/30 bg-info/10 text-info">
                    <span className="size-1.5 animate-pulse rounded-full bg-info" /> still going
                  </Pill>
                )}
              </div>
              <p className="mt-1.5 text-[14px] leading-relaxed text-foreground-secondary">{huddleExplainer(huddle.leader)}</p>
              <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-foreground-secondary">
                <span className="inline-flex items-center gap-1.5">
                  <MemberAvatar slug={huddle.leader} hue="var(--foreground-secondary)" size="sm" />
                  led by <span className="font-medium text-foreground">{who(huddle.leader)}</span>
                </span>
                <span className="text-muted-foreground">·</span>
                <span>with {andList(columns(huddle))}</span>
                <span className="text-muted-foreground">·</span>
                <span>started {relativeAge(huddle.created_at, now, { seconds: false })}</span>
                {!huddle.finished && huddle.deadline_at && (
                  <>
                    <span className="text-muted-foreground">·</span>
                    <span className={due ? '' : 'text-warning'}>{due ? `answers due in ${due}` : 'answers are late'}</span>
                  </>
                )}
              </p>
            </div>
            <Stepper huddle={huddle} />
          </div>
          {huddle.checkin && <CheckinLine checkin={huddle.checkin} />}
          <PrioritiesBrief brief={huddle.priorities_brief ?? ''} />
        </header>

        {/* A huddle filed before proposed projects existed shows its loose tasks as before. */}
        {(huddle.projects ?? []).length + (huddle.tasks ?? []).length > 0 ? (
          <HuddleProposals huddle={huddle} onChanged={() => setVersion((v) => v + 1)} />
        ) : (
          <HuddleOutcome huddle={huddle} compact={story} />
        )}

        <section aria-label="How it went" className="mt-10 border-t border-border pt-6">
          <HuddleConversation huddle={huddle} />
        </section>
        {error && <p className="mt-4 text-[12px] text-warning">Live refresh paused: {error}</p>}
      </div>
    </BriefContext.Provider>
  )
}

export default HuddlePage
