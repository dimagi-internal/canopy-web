import { describe, expect, it } from 'vitest'
import {
  DEFAULT_FILTERS,
  NO_RUNNER,
  buildTree,
  childLinks,
  deleteConsequence,
  folderKey,
  linkKind,
  matches,
  sessionMode,
  sessionReference,
  sessionState,
  shortAge,
  type IdeFilters,
  type IdeSession,
} from './ideTree'

const s = (over: Partial<IdeSession>): IdeSession => ({
  id: over.id ?? Math.random().toString(36),
  title: 't',
  status: 'active',
  last_activity_at: '2026-10-16T12:00:00Z',
  ...over,
})

const with_ = (over: Partial<IdeFilters>): IdeFilters => ({ ...DEFAULT_FILTERS, ...over })

describe('sessionState', () => {
  it('reads a pending question as input, even while the runner says running', () => {
    expect(sessionState(s({ waiting_on_you: true, running: true }))).toBe('input')
    expect(sessionState(s({ running: true }))).toBe('running')
    expect(sessionState(s({}))).toBe('idle')
  })

  it('reads a session behind an offline or paused runner as held, never running', () => {
    expect(sessionState(s({ running: true, runner_online: false, runner_status: 'disconnected' }))).toBe('held')
    expect(sessionState(s({ waiting_on_you: true, runner_online: false, runner_status: 'paused' }))).toBe('held')
    // Unbound (no runner yet) is not held.
    expect(sessionState(s({ running: true, runner_online: null }))).toBe('running')
  })
})

describe('matches — held', () => {
  const held = s({ running: true, runner_online: false, runner_status: 'paused' })

  it('hides held sessions only when held is switched off', () => {
    expect(matches(held, DEFAULT_FILTERS)).toBe(true)
    expect(matches(held, with_({ states: { ...DEFAULT_FILTERS.states, held: false } }))).toBe(false)
    // Switching running off does not hide it: it is not running.
    expect(matches(held, with_({ states: { ...DEFAULT_FILTERS.states, running: false } }))).toBe(true)
  })

  it('still shows held sessions under a filter saved before held existed', () => {
    const old = { running: true, input: true, idle: true } as unknown as IdeFilters['states']
    expect(matches(held, with_({ states: old }))).toBe(true)
  })

  it('counts held sessions in their folder', () => {
    const tree = buildTree([{ ...held, emdash_project: 'p' }], DEFAULT_FILTERS)
    expect(tree[0].counts.held).toBe(1)
    expect(tree[0].counts.running).toBe(0)
  })
})

describe('sessionMode', () => {
  it('is none until a turn has been claimed', () => {
    expect(sessionMode(s({ turn_mode: '' }))).toBe('none')
    expect(sessionMode(s({ turn_mode: 'manual' }))).toBe('manual')
  })
})

describe('folderKey', () => {
  it('prefers the emdash project, then project, then the agent', () => {
    expect(folderKey(s({ emdash_project: 'canopy-web', agent_slug: 'hal' }))).toBe('canopy-web')
    expect(folderKey(s({ project: 'ace-web' }))).toBe('ace-web')
    expect(folderKey(s({ agent_slug: 'eva' }))).toBe('eva')
  })
})

describe('matches', () => {
  it('hides a switched-off runner, and files unbound sessions under NO_RUNNER', () => {
    expect(matches(s({ runner_id: 'r1' }), with_({ hiddenRunners: ['r1'] }))).toBe(false)
    expect(matches(s({ runner_id: 'r2' }), with_({ hiddenRunners: ['r1'] }))).toBe(true)
    expect(matches(s({ runner_id: null }), with_({ hiddenRunners: [NO_RUNNER] }))).toBe(false)
  })

  it('filters by state and mode independently', () => {
    const states = { running: true, input: false, held: true, idle: true }
    expect(matches(s({ waiting_on_you: true }), with_({ states }))).toBe(false)
    const modes = { manual: true, auto: false, none: true }
    expect(matches(s({ turn_mode: 'auto' }), with_({ modes }))).toBe(false)
    expect(matches(s({ turn_mode: 'manual' }), with_({ modes }))).toBe(true)
  })

  it('shows only your own sessions when asked', () => {
    expect(matches(s({ started_by_you: false }), DEFAULT_FILTERS)).toBe(true)
    expect(matches(s({ started_by_you: false }), with_({ mineOnly: true }))).toBe(false)
    expect(matches(s({ started_by_you: true }), with_({ mineOnly: true }))).toBe(true)
  })

  it('hides archived sessions unless asked for', () => {
    expect(matches(s({ status: 'archived' }), DEFAULT_FILTERS)).toBe(false)
    expect(matches(s({ status: 'archived' }), with_({ archived: true }))).toBe(true)
  })

  it('searches title, folder and runner', () => {
    const row = s({ title: 'fix glob', emdash_project: 'canopy-web', runner_name: 'jj-air' })
    expect(matches(row, with_({ query: 'GLOB' }))).toBe(true)
    expect(matches(row, with_({ query: 'canopy' }))).toBe(true)
    expect(matches(row, with_({ query: 'jj-air' }))).toBe(true)
    expect(matches(row, with_({ query: 'nope' }))).toBe(false)
  })
})

describe('buildTree', () => {
  it('folds into A→Z folders, unfiled last, newest activity first, with counts', () => {
    const rows = [
      s({ id: 'a', emdash_project: 'zeta', last_activity_at: '2026-10-16T10:00:00Z' }),
      s({ id: 'b', emdash_project: 'alpha', running: true, last_activity_at: '2026-10-16T09:00:00Z' }),
      s({ id: 'c', emdash_project: 'alpha', waiting_on_you: true, last_activity_at: '2026-10-16T11:00:00Z' }),
      s({ id: 'd', last_activity_at: '2026-10-16T12:00:00Z' }),
    ]
    const tree = buildTree(rows, DEFAULT_FILTERS)
    expect(tree.map((f) => f.key)).toEqual(['alpha', 'zeta', ''])
    expect(tree[0].sessions.map((r) => r.id)).toEqual(['c', 'b'])
    expect(tree[0].counts).toEqual({ running: 1, input: 1, held: 0, idle: 0 })
  })

  it('marks agent folders', () => {
    const tree = buildTree([s({ agent_slug: 'hal', emdash_project: 'hal' }), s({ emdash_project: 'repo' })], DEFAULT_FILTERS)
    expect(tree.find((f) => f.key === 'hal')?.isAgent).toBe(true)
    expect(tree.find((f) => f.key === 'repo')?.isAgent).toBe(false)
  })

  it('keeps a fork in its own place, sorted by its own activity', () => {
    const rows = [
      s({ id: 'src', emdash_project: 'repo', last_activity_at: '2026-10-16T08:00:00Z' }),
      s({ id: 'other', emdash_project: 'repo', last_activity_at: '2026-10-16T10:00:00Z' }),
      s({ id: 'fork', emdash_project: 'repo', parent_session_id: 'src', last_activity_at: '2026-10-16T11:00:00Z' }),
    ]
    const [folder, ...rest] = buildTree(rows, DEFAULT_FILTERS)
    expect(rest).toEqual([])
    expect(folder.sessions.map((r) => r.id)).toEqual(['fork', 'other', 'src'])
  })

  it('drops folders whose every session is filtered out', () => {
    const tree = buildTree([s({ emdash_project: 'x', runner_id: 'r1' })], with_({ hiddenRunners: ['r1'] }))
    expect(tree).toEqual([])
  })
})

describe('childLinks', () => {
  const rows = [
    s({ id: 'src', emdash_project: 'repo' }),
    s({ id: 'fork', emdash_project: 'repo', parent_session_id: 'src', provenance: { forked_from: 'src' }, last_activity_at: '2026-10-16T11:00:00Z' }),
    s({ id: 'job', emdash_project: 'elsewhere', runner_id: 'r1', parent_session_id: 'src', last_activity_at: '2026-10-16T12:00:00Z' }),
    s({ id: 'old', parent_session_id: 'src', status: 'archived' }),
    s({ id: 'self', parent_session_id: 'self' }),
  ]

  it('links every session started from a parent under it, newest first, wherever it lives', () => {
    expect(childLinks(rows, DEFAULT_FILTERS).get('src')?.map((r) => r.id)).toEqual(['job', 'fork'])
    expect(childLinks(rows, DEFAULT_FILTERS).has('self')).toBe(false)
  })

  it('still links a child whose own row the filters hide, and an archived one only when archived is shown', () => {
    expect(childLinks(rows, with_({ hiddenRunners: ['r1'] })).get('src')?.map((r) => r.id)).toContain('job')
    expect(childLinks(rows, with_({ archived: true })).get('src')?.map((r) => r.id)).toContain('old')
  })

  it('says a fork is a fork', () => {
    expect(linkKind(rows[1])).toBe('fork')
    expect(linkKind(rows[2])).toBe('started')
  })
})

describe('shortAge', () => {
  const now = new Date('2026-10-16T12:00:00Z')
  it('is compact', () => {
    expect(shortAge('2026-10-16T11:59:30Z', now)).toBe('now')
    expect(shortAge('2026-10-16T11:48:00Z', now)).toBe('12m')
    expect(shortAge('2026-10-16T09:00:00Z', now)).toBe('3h')
    expect(shortAge('2026-10-14T12:00:00Z', now)).toBe('2d')
  })
})

describe('deleteConsequence', () => {
  it('names an emdash task only when a runner reports one', () => {
    expect(deleteConsequence(s({ runner_name: 'acedimagi-mbp-cdp', runner_reported: true }))).toBe(
      'Deletes its emdash task on acedimagi-mbp-cdp.',
    )
    expect(deleteConsequence(s({ runner_name: 'cloud-ec2-2', runner_reported: false }))).toBe(
      'Cancels its turns on cloud-ec2-2 and archives it.',
    )
    expect(deleteConsequence(s({}))).toBe('Archives it (it has nothing on a runner).')
  })
})

describe('sessionReference', () => {
  it('links the session page under its workspace, titled for the people reading', () => {
    expect(sessionReference({ id: 'abc', workspace: 'dimagi' }, 'Fix the runner', 'https://canopy.test')).toBe(
      '[Fix the runner](https://canopy.test/w/dimagi/sessions/abc)',
    )
  })

  it('escapes brackets so a title cannot break the link', () => {
    expect(sessionReference({ id: 'abc', workspace: 'dimagi' }, '[wip] a]b', 'https://canopy.test')).toBe(
      '[\\[wip\\] a\\]b](https://canopy.test/w/dimagi/sessions/abc)',
    )
  })
})
