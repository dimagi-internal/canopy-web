// @vitest-environment jsdom
//
// A machine can run several runners; the table says what each serves
// row is and, when a machine runs several, which machine they share.
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import type { TopologyRunnerOut } from '@/api/workspaces'
import { RunnersTable } from './RunnersTable'

function row(name: string, over: Partial<TopologyRunnerOut> = {}): TopologyRunnerOut {
  return {
    id: name, name, kind: 'cloud', location: 'cloud', status: 'online', ready: true, ready_note: '',
    paused: false, host: name, last_heartbeat_at: null, workspace: 'dimagi', in_tree: true,
    owner_email: null, flags: [], agent_count: 0, code_current: null, problems: [], status_note: '',
    remedy: '', code_sha: '', expected_code_sha: '', agents: [], usage_reported_at: null,
    can_administer: false, machine: '', serves: '',
    ...over,
  } as TopologyRunnerOut
}

afterEach(cleanup)

describe('RunnersTable runners on one machine', () => {
  it('says what each serves, and the machine only when it runs several', () => {
    render(
      <MemoryRouter>
        <RunnersTable workspace="dimagi" runners={[
          row('cloud-ec2-2-agent-eva', { machine: 'ec2-2-role', serves: 'agent:eva' }),
          row('cloud-ec2-2-dimagi', { machine: 'ec2-2-role', serves: 'tree:dimagi' }),
          row('cloud-ec2-9', { machine: 'ec2-9-role', serves: 'tree:dimagi' }),
          row('jj-mbp', { kind: 'emdash' }),
        ]} />
      </MemoryRouter>,
    )
    const cell = (name: string) => within(document.querySelector(`[data-runner="${name}"]`) as HTMLElement)
    expect(cell('cloud-ec2-2-agent-eva').getByTestId('runner-serves').textContent).toBe('· only agent eva')
    expect(cell('cloud-ec2-2-dimagi').getByTestId('runner-machine').textContent).toBe('· on ec2-2-role')
    expect(cell('cloud-ec2-9').queryByTestId('runner-machine')).toBeNull()
    expect(cell('jj-mbp').queryByTestId('runner-serves')).toBeNull()
    expect(screen.getAllByTestId('runner-machine')).toHaveLength(2)
  })
})

describe('RunnersTable checks and detail', () => {
  const box = row('cloud-ec2-2', {
    can_administer: true,
    agents: [{ slug: 'jarvis', name: 'Jarvis', workspace: 'dimagi' }],
    problems: [
      { name: 'claude.probe.subscription-1', status: 'fail', detail: 'not signed in' },
      { name: 'github.jarvis', status: 'fail', detail: 'no token lent' },
      { name: 'github.ace', status: 'warn', detail: 'token expires soon' },
    ],
  })

  it('counts the checks in the row instead of listing every name', () => {
    render(<MemoryRouter><RunnersTable workspace="dimagi" runners={[box]} /></MemoryRouter>)
    expect(screen.getByTestId('runner-checks').textContent).toBe('2 failing · 1 warning')
  })

  it('leads the detail with the way to change the runner, and links each check to its fix', () => {
    render(<MemoryRouter><RunnersTable workspace="dimagi" runners={[box]} open="cloud-ec2-2" /></MemoryRouter>)
    const manage = screen.getByTestId('runner-manage')
    expect(manage.textContent).toContain('Manage runner')
    expect(manage.getAttribute('href')).toBe('/w/dimagi/runners/cloud-ec2-2')
    const fixes = screen.getAllByText('Fix →').map((a) => a.getAttribute('href'))
    // The sign-in is fixed on the runner, the agent's token on the agent; an
    // agent outside this tree (ace) has nowhere to link.
    expect(fixes).toEqual(['/w/dimagi/runners/cloud-ec2-2', '/w/dimagi/agents/jarvis/settings#credentials'])
  })
})
