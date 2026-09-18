import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useOutletContext, useSearchParams } from 'react-router-dom'
import { getSkillHistory, syncSkillHistory, type SkillHistoryOut } from '@/api/agents'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { WorkbenchSkeleton } from 'canopy-ui'
import { describeSelection } from '@/widget/pageState'
import { usePageAction } from '@/widget/usePageAction'
import { usePageState } from '@/widget/usePageState'
import { useResource } from '@/widget/useResource'
import { SkillHistoryChart } from './skillHistory/SkillHistoryChart'
import { SkillHistoryLanes } from './skillHistory/SkillHistoryLanes'
import { SkillHistoryPanel } from './skillHistory/SkillHistoryPanel'
import { buildModel, dayOf, fmtDay, isoOf, panelAt, scopeNames, tilesAt, totalsAt, type Selection } from './skillHistory/model'

const STATE_COPY: Record<string, string> = {
  no_repo: 'This agent has no repository configured, so there is no history to read.',
  no_owner: 'History reads through the agent owner’s GitHub connection, and this agent has no owner.',
  owner_not_connected: 'History reads through the agent owner’s GitHub connection. The owner hasn’t connected GitHub.',
  repo_not_granted: 'The owner’s GitHub connection can’t reach this agent’s repository. The owner can add it on GitHub’s installation screen.',
}

// Small inline stroke icons — not text glyphs (▶/❚❚ render as emoji on some
// platforms, and this app forbids emoji in UI copy).
function PlayIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden="true">
      <path d="M7 4.5v15l13-7.5z" />
    </svg>
  )
}
function PauseIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden="true">
      <rect x="6" y="4.5" width="4.5" height="15" />
      <rect x="13.5" y="4.5" width="4.5" height="15" />
    </svg>
  )
}
function ChevronLeftIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="15 6 9 12 15 18" />
    </svg>
  )
}
function ChevronRightIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="9 6 15 12 9 18" />
    </svg>
  )
}

const LOAD_ERROR_COPY = "Couldn't load this agent's history."
const SYNC_ERROR_COPY = 'Sync failed. Try again in a moment.'

export function AgentHistorySection() {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [data, setData] = useState<SkillHistoryOut | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [syncError, setSyncError] = useState<string | null>(null)
  const [params, setParams] = useSearchParams()
  const [playing, setPlaying] = useState(false)
  const timer = useRef<number | null>(null)

  // Every request that can set `data` (the initial load, a Retry, a sync)
  // gets its own sequence number. Only the result belonging to the most
  // RECENTLY ISSUED request is ever applied, regardless of resolution order —
  // otherwise a slow initial GET that resolves after a faster sync already
  // landed newer data would silently stomp it with stale data.
  const seq = useRef(0)

  const load = () => {
    const id = ++seq.current
    setLoadError(null)
    getSkillHistory(agent.slug)
      .then((h) => {
        if (id !== seq.current) return
        setData(h)
        setLoadError(null)
      })
      .catch(() => {
        if (id !== seq.current) return
        setLoadError(LOAD_ERROR_COPY)
      })
  }

  useEffect(() => {
    setData(null)
    setLoadError(null)
    setSyncError(null)
    load()
    // `load` closes over `agent.slug`; re-running only on slug change is
    // deliberate, and a stale in-flight request for the previous agent is
    // automatically superseded the moment this call bumps `seq`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent.slug])

  const model = useMemo(() => (data ? buildModel(data) : null), [data])
  const sel: Selection = {
    ...(params.get('group') ? { group: params.get('group')! } : {}),
    ...(params.get('skill') ? { skill: params.get('skill')! } : {}),
    ...(params.get('commit') ? { commit: params.get('commit')! } : {}),
  }

  // Kept in sync every render so a playback tick — whose interval closure
  // can be arbitrarily stale — always reads the CURRENT selection, never the
  // one that was live when play() was called.
  const selRef = useRef(sel)
  selRef.current = sel

  const at = params.get('at')
  let day = 0
  if (model) {
    const parsed = at ? dayOf(model, at) : model.days
    day = Math.min(model.days, Math.max(0, Number.isFinite(parsed) ? parsed : model.days))
  }

  const stop = () => {
    if (timer.current) window.clearInterval(timer.current)
    timer.current = null
    setPlaying(false)
  }
  useEffect(() => () => stop(), [])

  // A selection change (group/skill/commit, or the "All skills" breadcrumb)
  // PUSHES a history entry so Back walks the drill-down one step at a time —
  // and stops any playback in progress first, so the next 60ms tick can never
  // rewrite this selection with whatever was live when play() started.
  const onSelect = (next: Selection) => {
    stop()
    const p = new URLSearchParams()
    if (next.group) p.set('group', next.group)
    if (next.skill) p.set('skill', next.skill)
    if (next.commit) p.set('commit', next.commit)
    if (model && day < model.days) p.set('at', isoOf(model, day))
    setParams(p, { replace: false })
  }

  // A date-only move (slider scrub, ±week, Latest, a chart click, or a
  // playback tick) REPLACES the current history entry instead — playback
  // alone rewrites the URL every 60ms, which would otherwise flood back/
  // forward history with one entry per tick. Reads the selection from
  // `selRef`, never a captured closure, so a stale interval tick can't
  // reintroduce an old selection either.
  const writeDay = (nextDay: number) => {
    const current = selRef.current
    const p = new URLSearchParams()
    if (current.group) p.set('group', current.group)
    if (current.skill) p.set('skill', current.skill)
    if (current.commit) p.set('commit', current.commit)
    if (model && nextDay < model.days) p.set('at', isoOf(model, nextDay))
    setParams(p, { replace: true })
  }

  const onDay = (d: number) => {
    stop()
    if (!model) return
    writeDay(Math.max(0, Math.min(model.days, d)))
  }

  const resource = `skill-history://${agent.slug}`
  const asOf = model ? isoOf(model, day) : null

  // The selection, not the data: the assistant resolves it through the
  // `skill_history` tool with the caller's own access. Rides the first message
  // and is re-read via `current_page` later (embedding doc §5).
  usePageState(
    () => describeSelection({
      backingTool: 'skill_history',
      resource,
      ids: sel.commit ? [sel.commit] : sel.skill ? [sel.skill] : sel.group ? [sel.group] : [],
      filters: { agent: agent.slug, group: sel.group ?? null, skill: sel.skill ?? null, commit: sel.commit ?? null, as_of: asOf },
    }),
    [agent.slug, sel.group, sel.skill, sel.commit, asOf],
  )

  // A sync from another tab, or one the assistant triggered, repaints this one.
  useResource(resource, load)

  usePageAction('selectSkill', ({ skill }) => {
    if (!model || typeof skill !== 'string' || !model.skills.has(skill)) throw new Error(`no skill named ${String(skill)} in ${agent.slug}'s history`)
    onSelect({ skill })
    return { selected: skill }
  }, {
    description: 'Open one skill’s history on the page the user is viewing',
    parameters: { type: 'object', properties: { skill: { type: 'string' } }, required: ['skill'] },
  })

  usePageAction('showCommit', ({ sha }) => {
    const full = model && typeof sha === 'string' ? model.h.commits.find((c) => c.sha.startsWith(sha))?.sha : undefined
    if (!full) throw new Error(`no commit ${String(sha)} in ${agent.slug}'s skill history`)
    onSelect({ commit: full })
    return { shown: full }
  }, {
    description: 'Open one commit on the page the user is viewing (full or abbreviated sha)',
    parameters: { type: 'object', properties: { sha: { type: 'string' } }, required: ['sha'] },
  })

  usePageAction('setTimeline', ({ date }) => {
    if (!model || typeof date !== 'string') throw new Error('date must be YYYY-MM-DD')
    const d = dayOf(model, date)
    if (Number.isNaN(d) || d < 0 || d > model.days) throw new Error(`${date} is outside this history (${isoOf(model, 0)} to ${isoOf(model, model.days)})`)
    writeDay(d)
    return { date }
  }, {
    description: 'Move the page’s timeline to a date (YYYY-MM-DD)',
    parameters: { type: 'object', properties: { date: { type: 'string' } }, required: ['date'] },
  })

  if (!data || !model) {
    if (loadError) {
      return (
        <div className="flex flex-col items-start gap-3 px-6 py-8">
          <p className="text-[13px] text-muted-foreground">{loadError}</p>
          <button type="button" onClick={load} className="rounded-lg border border-border px-3 py-1.5 text-[13px] hover:border-primary">
            Retry
          </button>
        </div>
      )
    }
    return <div className="px-6 py-8"><WorkbenchSkeleton /></div>
  }

  const totals = totalsAt(model, day)
  const scope = scopeNames(model, sel)
  const panel = panelAt(model, day, sel)
  const crumbs: { label: string; sel: Selection | null }[] = [{ label: 'All skills', sel: {} }]
  const skillGroup = sel.skill ? model.skills.get(sel.skill)?.group ?? null : null
  if (sel.group) crumbs.push({ label: sel.group, sel: { group: sel.group } })
  if (sel.skill) {
    if (skillGroup !== null) crumbs.push({ label: model.h.groups[skillGroup].title, sel: { group: model.h.groups[skillGroup].title } })
    crumbs.push({ label: sel.skill, sel: { skill: sel.skill } })
  }
  if (sel.commit) crumbs.push({ label: `commit ${sel.commit.slice(0, 8)}`, sel: { commit: sel.commit } })

  const play = () => {
    if (playing) {
      stop()
      return
    }
    let d = day >= model.days ? 0 : day
    setPlaying(true)
    timer.current = window.setInterval(() => {
      d += 1
      if (d >= model.days) {
        stop()
        writeDay(model.days)
      } else {
        writeDay(d)
      }
    }, 60)
  }

  const sync = async () => {
    const id = ++seq.current
    setSyncing(true)
    setSyncError(null)
    try {
      const h = await syncSkillHistory(agent.slug)
      if (id === seq.current) {
        setData(h)
        setLoadError(null)
      }
    } catch {
      if (id === seq.current) setSyncError(SYNC_ERROR_COPY)
    } finally {
      setSyncing(false)
    }
  }

  return (
    <div className="flex flex-col gap-6 px-6 py-8">
      <header className="flex flex-wrap items-end justify-between gap-6">
        <div className="flex max-w-3xl flex-col gap-2">
          <h1 className="m-0 text-[28px] font-semibold text-foreground">How {agent.name}’s skills changed</h1>
          <p className="m-0 text-[14px] text-foreground-secondary">
            Every skill in the repository, read from its git history. A revision is a commit that changed the skill’s SKILL.md.
            Select a group, a skill, a week on the chart or a commit.
          </p>
          <p className="m-0 text-[12px] text-muted-foreground">
            {data.synced_at ? `Read ${new Date(data.synced_at).toLocaleString()} using ${data.synced_with}’s GitHub access.` : 'Not read yet.'}
            {' '}
            <button type="button" onClick={sync} disabled={syncing} className="text-primary underline disabled:opacity-50">
              {syncing ? 'Syncing…' : 'Sync from GitHub'}
            </button>
            {syncError && <span className="ml-2 text-[12px] text-destructive">{syncError}</span>}
          </p>
          {data.credential_state !== 'ok' && STATE_COPY[data.credential_state] && (
            <p role="status" className="m-0 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-[13px] text-warning">
              {STATE_COPY[data.credential_state]}
              {(data.credential_state === 'owner_not_connected' || data.credential_state === 'repo_not_granted') && (
                <>
                  {' '}
                  <Link to="/settings" className="underline">GitHub settings</Link>
                </>
              )}
            </p>
          )}
          {data.last_error && data.credential_state === 'ok' && (
            <p role="status" className="m-0 text-[12px] text-destructive">Last sync failed: {data.last_error}</p>
          )}
        </div>
        <dl className="m-0 grid grid-cols-2 gap-6 sm:grid-cols-4">
          {([[totals.skills, 'skills'], [totals.revisions.toLocaleString(), 'revisions'], [totals.withChecks, 'skills with a QA or eval skill'], [totals.removed, 'skills removed']] as const).map(([v, l]) => (
            <div key={l} className="flex flex-col">
              <dd className="m-0 text-[32px] font-semibold leading-none text-foreground">{v}</dd>
              <dt className="text-[12px] text-muted-foreground">{l}</dt>
            </div>
          ))}
        </dl>
      </header>

      <div className="flex items-center gap-3 rounded-xl border border-border bg-card px-4 py-3">
        <button type="button" onClick={play} aria-label={playing ? 'Pause' : 'Play from the first commit'}
                className="flex h-11 w-11 items-center justify-center rounded-full bg-primary text-primary-foreground">
          {playing ? <PauseIcon /> : <PlayIcon />}
        </button>
        <button type="button" aria-label="Back one week" onClick={() => onDay(day - 7)} className="flex h-11 w-11 items-center justify-center rounded-lg border border-border">
          <ChevronLeftIcon />
        </button>
        <button type="button" aria-label="Forward one week" onClick={() => onDay(day + 7)} className="flex h-11 w-11 items-center justify-center rounded-lg border border-border">
          <ChevronRightIcon />
        </button>
        <label htmlFor="history-day" className="w-16 font-mono text-[14px]">{fmtDay(model, day)}</label>
        <input id="history-day" type="range" min={0} max={model.days} value={day} onChange={(e) => onDay(Number(e.target.value))} className="flex-grow accent-primary" />
        <button type="button" onClick={() => onDay(model.days)} className="h-11 rounded-lg border border-border px-3 text-[13px]">Latest</button>
      </div>

      <div className="flex flex-col gap-8 lg:flex-row lg:items-start">
        <div className="flex min-w-0 flex-grow flex-col gap-6">
          <SkillHistoryChart model={model} day={day} scope={scope} onDay={onDay}
                             scopeLabel={sel.commit ? `skills in commit ${sel.commit.slice(0, 8)}` : sel.skill ?? sel.group ?? 'all skills'} />
          <SkillHistoryLanes rows={tilesAt(model, day, sel)} onSelect={onSelect} />
          <p className="text-[12px] text-muted-foreground">Groups come from the agent files in the repository. A QA or eval skill is shown inside the skill it checks. Bars are 1px per revision, capped at 90.</p>
        </div>
        <SkillHistoryPanel panel={panel} crumbs={crumbs} dateLabel={fmtDay(model, day)} onSelect={onSelect} onDay={onDay}
                           commitDay={(sha) => {
                             const idx = model.bySha.get(sha)
                             return idx === undefined ? null : model.commitDay[idx]
                           }} />
      </div>
    </div>
  )
}
