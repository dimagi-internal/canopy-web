// @vitest-environment jsdom
// The Session Context and Turn Context sections of the session panel, in every state they can be in.
// The fixtures are trimmed from what the local server's routes really returned for each case.
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getSessionInfo = vi.fn()
const getSessionContext = vi.fn()
const getTurnContext = vi.fn()
vi.mock('@/api/chat', () => ({
  getSessionInfo: (...a: unknown[]) => getSessionInfo(...a),
  getSessionContext: (...a: unknown[]) => getSessionContext(...a),
  getTurnContext: (...a: unknown[]) => getTurnContext(...a),
}))

const { SessionInfoPanel } = await import('./SessionInfoPanel')
const { decidingStep, shortLabel, basisText, outcomeText } = await import('./turnContextWords')

const TURN = {
  id: 't-1', origin: 'canopy_web_chat', status: 'done', prompt: 'hello', result_note: '', content_hidden: false,
  created_at: '2026-11-05T10:00:00Z', claimed_at: null, finished_at: null, session_id: '', claimed_by_name: 'box-one',
  enqueued_by_email: 'owner@dimagi.com',
  initiator: { kind: 'user', via: 'canopy_web_chat', assurance: 'session', self: true, client: '', user: { id: 1, email: 'owner@dimagi.com', name: 'owner' } },
  turn_mode: 'auto', turn_mode_basis: 'agent', provenance: {}, parent_turn_id: null, parent_session_id: null,
  parent_task: '', parent_claude_session: '',
}
const INFO = {
  session: {
    id: 's1', title: 'Owner: whole agent', agent_slug: 'agent', project: '', workspace: 'connect', status: 'active',
    origin: 'web', created_at: '2026-11-05T10:00:00Z', last_activity_at: null, turn_mode: 'auto',
    turn_origin: 'canopy_web_chat', runner_status: null, created_by: 'owner@dimagi.com', provenance: {},
    parent_turn_id: null, parent_session_id: null, parent_task: '', parent_claude_session: '', activity: {}, slack: null,
  },
  metadata: {}, contact: null, updated_at: '2026-11-05T10:00:00Z', binding: null,
  turns: [TURN, { ...TURN, id: 't-2' }], turn_count: 2, turns_limit: 50, participant_count: 0,
  github_identity: { agent: 'agent', source: 'agent', repo: '' }, message_count: 0,
}

const PENDING = (name: string) => ({ name, kind: 'x', gets: 'pending', why: '' })
const REVIEW = { mode: 'pending', basis: '', manual_only: false, may_request_auto: true, may_pin_runner: true }
const OWNER_NOW = {
  standing: { relationship: 'owner', permissions: [], agent_role: 'owner', source: 'owner', via: '' },
  access: 'full', basis: 'owner',
  profile: { kind: 'whole_agent', name: '', site: null, description: null, entry: null, tools: null, bash: null,
    read_paths: null, write_paths: null, data: null, default_profile_version: null },
  session_key_suffix: '',
  credentials: ['canopy_login', 'github_identity', 'caller_token', 'agent_vault'].map(PENDING),
  review: REVIEW, stamped: '', refusal: null,
  trace: [
    { rule: 'send_blocks', outcome: 'none', detail: '' },
    { rule: 'trusted_without_a_list', outcome: 'owner', detail: '' },
    { rule: 'sensitive', outcome: 'not sensitive', detail: '' },
  ],
}
const READ_MODE = {
  ...OWNER_NOW,
  standing: { relationship: 'member', permissions: ['read'], agent_role: 'read_only', source: 'list:person:4', via: 'reader@dimagi.com' },
  access: 'confined', basis: 'capability:ask',
  profile: { ...OWNER_NOW.profile, kind: 'read_mode', name: 'ask', description: 'Ask', tools: ['Read'] },
  session_key_suffix: '#ask', stamped: 'ask',
  review: { ...REVIEW, may_request_auto: false, may_pin_runner: false },
  trace: [
    { rule: 'send_blocks', outcome: 'none', detail: '' },
    { rule: 'trusted_without_a_list', outcome: 'no', detail: 'relationship member' },
    { rule: 'session_grants', outcome: 'none', detail: '' },
    { rule: 'access_list', outcome: 'read', detail: 'grant:person:4' },
    { rule: 'entry', outcome: 'ask', detail: 'read mode: ask' },
    { rule: 'sensitive', outcome: 'not sensitive', detail: '' },
  ],
}
const REFUSED = {
  ...OWNER_NOW,
  standing: { relationship: 'member', permissions: [], agent_role: null, source: '', via: '' },
  access: 'refused', basis: 'refused', profile: null, stamped: null,
  credentials: [{ name: 'agent_vault', kind: 'vault', gets: 'no', why: 'a refused turn does not run' }],
  refusal: { code: 'not_on_access_list', why: "you are not on agent's access list",
    how_to_fix: 'Have owner@dimagi.com or an admin of agent add you to its access list as read only (to put questions to it).',
    who_can_fix: 'owner@dimagi.com or an admin of agent' },
  trace: [
    { rule: 'send_blocks', outcome: 'none', detail: '' },
    { rule: 'trusted_without_a_list', outcome: 'no', detail: 'relationship member' },
    { rule: 'session_grants', outcome: 'none', detail: '' },
    { rule: 'access_list', outcome: 'holds nothing', detail: '' },
    { rule: 'sensitive', outcome: 'not sensitive', detail: '' },
  ],
}
const FACTS = {
  session_id: 's1', agent: 'agent', workspace: 'connect', channel: 'canopy_web_chat', runner_requested: null,
  runner_bound: null, site: '', page: '', sensitive: false, sensitive_requirement: '', is_review: false, review_of: null,
  audience: { creator: [1], participants: [], agent_admins: [2], coaches: {}, project_teams: {} },
  participants: [], session_grants: [],
  send_blocks: [{ code: 'cannot_write', why: 'you can read this session but not act in it', scope: 'some_people' }],
}
const ranAs = (ctx: object, extra: object = {}) => ({
  ...ctx, version: 1, moment: 'claim', recorded_at: '2026-11-05T10:00:00Z',
  runner: { id: 'r', name: 'box-one', kind: 'cloud' }, requested: null, admission: null, ...extra,
})

/** No new text may use the words the owner is removing. Server text (a refusal's `why`) is excluded by the fixtures. */
const BANNED = /\b(door|doors|ask|asks|capability|capabilities)\b/i

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function open(context: unknown) {
  getSessionInfo.mockResolvedValue(INFO)
  if (context instanceof Error) getSessionContext.mockRejectedValue(context)
  else getSessionContext.mockResolvedValue(context)
  render(<SessionInfoPanel sessionId="s1" />)
  await screen.findByTestId('session-info')
}

describe('Turn context: for you, now', () => {
  it('says Loading until the preview arrives', async () => {
    getSessionInfo.mockResolvedValue(INFO)
    getSessionContext.mockReturnValue(new Promise(() => {}))
    render(<SessionInfoPanel sessionId="s1" />)
    const now = await screen.findByTestId('turn-context-now')
    expect(now.textContent).toContain('Loading')
    expect(screen.queryByTestId('session-context')).toBeNull()
  })

  it('says why it could not load, and the rest of the panel still renders', async () => {
    await open(new Error('session not found'))
    expect((await screen.findByTestId('turn-context-error')).textContent).toBe('session not found')
    expect(screen.getByText('Where it came from')).toBeTruthy()
  })

  it('the owner: the whole agent, its reason, and the deciding step highlighted', async () => {
    await open({ session_context: FACTS, turn_context: OWNER_NOW, withheld: '' })
    const now = await screen.findByTestId('turn-context-now')
    expect(within(now).getByTestId('turn-context-headline').textContent).toBe('You can use the whole agent.')
    expect(now.textContent).toContain("the agent's owner")
    const steps = within(now).getByTestId('turn-context-trace').querySelectorAll('li')
    expect(steps).toHaveLength(3)
    expect(steps[1].getAttribute('data-deciding')).toBe('true')
    expect(steps[1].textContent).toContain('yes, the owner')
    expect(steps[1].textContent).toContain('decided it')
    expect(now.textContent).not.toMatch(BANNED)
  })

  it('read mode never shows its stored name', async () => {
    await open({ session_context: FACTS, turn_context: READ_MODE, withheld: '' })
    const now = await screen.findByTestId('turn-context-now')
    expect(now.textContent).toContain('Read mode only')
    expect(now.textContent).toContain('Read only')
    expect(now.textContent).toContain('reader@dimagi.com')
    expect(now.textContent).not.toMatch(BANNED)
    const deciding = now.querySelector('[data-deciding]')
    expect(deciding?.textContent).toContain('opens read mode')
  })

  it('refused: why, how to fix it and who can, the why said as a sentence', async () => {
    await open({ session_context: FACTS, turn_context: REFUSED, withheld: '' })
    const refused = await screen.findByTestId('turn-context-refused')
    expect(refused.textContent).toContain("You can't send a message here.")
    expect(screen.getByTestId('turn-context-why-not').textContent).toContain("You are not on agent's access list.")
    expect(screen.getByTestId('turn-context-how-to-fix').textContent).toContain('add you to its access list as read only')
    expect(screen.getByTestId('turn-context-who-can-fix').textContent).toContain('owner@dimagi.com or an admin of agent')
    expect(screen.getByTestId('turn-context-now').querySelector('[data-deciding]')?.textContent).toContain('holds nothing')
    expect(screen.queryByTestId('turn-context-credentials')).toBeNull()
    expect(screen.getByTestId('turn-context-now').textContent).toContain('nothing: a refused turn does not run')
  })

  it('a refusal recorded before refusals said how to fix them: no empty "how to fix" label', async () => {
    await open({ session_context: FACTS, turn_context: { ...REFUSED, refusal: { ...REFUSED.refusal, how_to_fix: '' } }, withheld: '' })
    const refused = await screen.findByTestId('turn-context-refused')
    expect(screen.queryByTestId('turn-context-how-to-fix')).toBeNull()
    expect(refused.textContent).not.toContain('How to fix')
  })

  it('withheld: says so and why, keeps the session facts, and breaks nothing', async () => {
    const why = "this conversation holds a host's sensitive data: only the people in it can see what a turn in it may do"
    await open({ session_context: { ...FACTS, sensitive: true, sensitive_requirement: 'sensitive_data' }, turn_context: null, withheld: why })
    const box = await screen.findByTestId('turn-context-withheld')
    expect(box.textContent).toContain('Withheld')
    expect(box.textContent).toContain(why)
    expect(screen.getByTestId('session-context-sensitive').textContent).toContain('Yes')
    expect(screen.queryByTestId('turn-context-trace')).toBeNull()
  })
})

describe('Session context', () => {
  it('every fact with a plain label', async () => {
    await open({ session_context: { ...FACTS, site: 'connect-labs', page: 'stock://a' }, turn_context: OWNER_NOW, withheld: '' })
    const facts = await screen.findByTestId('session-context')
    for (const text of ['agent', 'connect', 'canopy chat', 'connect-labs', 'stock://a', 'People who may only read it',
      '1 person is an admin of the agent', 'nobody added']) {
      expect(facts.textContent).toContain(text)
    }
    expect(facts.textContent).not.toMatch(BANNED)
  })
})

describe('Turn context: one turn', () => {
  it('reads nothing of a turn until it is opened, and reads it once', async () => {
    getTurnContext.mockResolvedValue({ turn_id: 't-1', status: 'recorded', ran_as: ranAs(OWNER_NOW), now: OWNER_NOW, differs: false, note: '' })
    await open({ session_context: FACTS, turn_context: OWNER_NOW, withheld: '' })
    expect(getSessionContext).toHaveBeenCalledTimes(1)
    expect(getTurnContext).not.toHaveBeenCalled()
    const row = screen.getAllByTestId('session-info-turn')[0]
    fireEvent.click(within(row).getAllByRole('button')[0])
    await within(row).findByTestId('turn-ran-as')
    fireEvent.click(within(row).getAllByRole('button')[0])
    fireEvent.click(within(row).getAllByRole('button')[0])
    expect(getTurnContext).toHaveBeenCalledTimes(1)
    expect(getTurnContext).toHaveBeenCalledWith('s1', 't-1')
    expect(within(row).getByTestId('turn-ran-as').textContent).toContain('the same')
  })

  it('says Loading while it is read', async () => {
    getTurnContext.mockReturnValue(new Promise(() => {}))
    await open({ session_context: FACTS, turn_context: OWNER_NOW, withheld: '' })
    const row = screen.getAllByTestId('session-info-turn')[0]
    fireEvent.click(within(row).getAllByRole('button')[0])
    expect(within(row).getByTestId('turn-ran-as-loading')).toBeTruthy()
  })

  it('says why it could not load, and asks again when reopened', async () => {
    getTurnContext.mockRejectedValueOnce(new Error('turn not found'))
    await open({ session_context: FACTS, turn_context: OWNER_NOW, withheld: '' })
    const row = screen.getAllByTestId('session-info-turn')[0]
    const button = within(row).getAllByRole('button')[0]
    fireEvent.click(button)
    expect((await within(row).findByTestId('turn-ran-as-error')).textContent).toBe('turn not found')
    getTurnContext.mockResolvedValue({ turn_id: 't-1', status: 'not_recorded', ran_as: null, now: OWNER_NOW, differs: false, note: 'not recorded' })
    fireEvent.click(button)
    fireEvent.click(button)
    await within(row).findByTestId('turn-ran-as')
    expect(getTurnContext).toHaveBeenCalledTimes(2)
  })

  it('an older turn: not recorded, and what it would be today', async () => {
    getTurnContext.mockResolvedValue({ turn_id: 't-2', status: 'not_recorded', ran_as: null, now: OWNER_NOW, differs: false, note: 'not recorded' })
    await open({ session_context: FACTS, turn_context: OWNER_NOW, withheld: '' })
    const row = screen.getAllByTestId('session-info-turn')[1]
    fireEvent.click(within(row).getAllByRole('button')[0])
    expect((await within(row).findByTestId('turn-ran-as-not-recorded')).textContent).toContain('not recorded')
    expect(within(row).getByTestId('turn-ran-as').textContent).toContain('the whole agent')
  })

  it('differs: "this turn ran as X; today it would be Y"', async () => {
    getTurnContext.mockResolvedValue({ turn_id: 't-1', status: 'recorded', ran_as: ranAs(READ_MODE), now: { ...OWNER_NOW, basis: 'grant:person:8' }, differs: true, note: '' })
    await open({ session_context: FACTS, turn_context: OWNER_NOW, withheld: '' })
    const row = screen.getAllByTestId('session-info-turn')[0]
    fireEvent.click(within(row).getAllByRole('button')[0])
    expect((await within(row).findByTestId('turn-ran-as-differs')).textContent)
      .toBe('This turn ran as read mode; today it would be the whole agent.')
    expect(within(row).getByTestId('turn-ran-as').textContent).not.toMatch(BANNED)
  })

  it('withheld: says why, and shows nothing of it', async () => {
    getTurnContext.mockResolvedValue({ turn_id: 't-1', status: 'withheld', ran_as: null, now: null, differs: false, note: 'only the people in it can see' })
    await open({ session_context: FACTS, turn_context: null, withheld: 'only the people in it can see' })
    const row = screen.getAllByTestId('session-info-turn')[0]
    fireEvent.click(within(row).getAllByRole('button')[0])
    const box = await within(row).findByTestId('turn-context-withheld')
    expect(box.textContent).toContain('only the people in it can see')
    expect(within(row).queryByTestId('turn-context-trace')).toBeNull()
  })

  it('a turn refused on arrival: says so, with the reason', async () => {
    getTurnContext.mockResolvedValue({ turn_id: 't-1', status: 'recorded', ran_as: ranAs(REFUSED, { moment: 'enqueue', runner: null, requested: 'connect' }), now: REFUSED, differs: false, note: '' })
    await open({ session_context: FACTS, turn_context: REFUSED, withheld: '' })
    const row = screen.getAllByTestId('session-info-turn')[0]
    fireEvent.click(within(row).getAllByRole('button')[0])
    const ran = await within(row).findByTestId('turn-ran-as')
    expect(ran.textContent).toContain('refused')
    expect(ran.textContent).toContain('when it was refused on arrival')
    expect(ran.textContent).toContain('"connect"')
  })

  it('Raw shows and copies what was read, the opened turn included', async () => {
    getTurnContext.mockResolvedValue({ turn_id: 't-1', status: 'not_recorded', ran_as: null, now: OWNER_NOW, differs: false, note: 'not recorded' })
    await open({ session_context: FACTS, turn_context: OWNER_NOW, withheld: '' })
    const row = screen.getAllByTestId('session-info-turn')[0]
    fireEvent.click(within(row).getAllByRole('button')[0])
    await within(row).findByTestId('turn-ran-as')
    fireEvent.click(screen.getByTestId('session-info-raw-toggle'))
    const raw = screen.getByTestId('session-info-raw').textContent ?? ''
    expect(raw).toContain('"trusted_without_a_list"')
    expect(raw).toContain('"turn_contexts"')
    expect(raw).toContain('"not_recorded"')
  })
})

describe('turnContextWords', () => {
  it('a send block decides a refusal it caused, even with later rules', () => {
    const ctx = {
      refusal: { code: 'cannot_write', why: '', how_to_fix: '', who_can_fix: '' },
      trace: [
        { rule: 'send_blocks', outcome: 'cannot_write', detail: '' },
        { rule: 'trusted_without_a_list', outcome: 'owner', detail: '' },
        { rule: 'sensitive', outcome: 'not sensitive', detail: '' },
      ],
    }
    expect(decidingStep(ctx)).toBe(0)
  })

  it('a sensitive refusal is decided by the sensitive rule', () => {
    const ctx = {
      refusal: { code: 'sensitive_not_open', why: '', how_to_fix: '', who_can_fix: '' },
      trace: [
        { rule: 'access_list', outcome: 'read', detail: '' },
        { rule: 'entry', outcome: 'ask', detail: '' },
        { rule: 'sensitive', outcome: 'refused', detail: '' },
      ],
    }
    expect(decidingStep(ctx)).toBe(2)
  })

  it('short labels and reasons stay plain', () => {
    expect(shortLabel({ access: 'confined', profile: { kind: 'connected_site', name: 'connect', site: 'connect-labs' } as never }))
      .toBe('"connect" on connect-labs')
    expect(shortLabel({ access: 'refused', profile: null })).toBe('refused')
    expect(basisText('capability:ask')).toBe('read mode, from the access list')
    expect(basisText('sensitive:data')).toContain('sensitive data')
    // A claim words an owner's confined turn as capability:<name>; that is not the access list.
    expect(basisText('capability:data', 'owner')).toBe('it was let in as "data"')
    expect(outcomeText({ rule: 'access_list', outcome: 'read,use:connect', detail: '' })).toBe('holds read, use "connect"')
  })
})
