// @vitest-environment jsdom
//
// A cloud box runs one runner per isolation unit; the table says which unit each
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
    missing_sessions: 0, can_administer: false, machine: '', unit: '',
    ...over,
  } as TopologyRunnerOut
}

afterEach(cleanup)

describe('RunnersTable units', () => {
  it('names the unit, and the machine only when it runs several', () => {
    render(
      <MemoryRouter>
        <RunnersTable workspace="dimagi" runners={[
          row('cloud-ec2-2-agent-eva', { machine: 'ec2-2-role', unit: 'agent:eva' }),
          row('cloud-ec2-2-dimagi', { machine: 'ec2-2-role', unit: 'tree:dimagi' }),
          row('cloud-ec2-9', { machine: 'ec2-9-role', unit: 'tree:dimagi' }),
          row('jj-mbp', { kind: 'emdash' }),
        ]} />
      </MemoryRouter>,
    )
    const cell = (name: string) => within(document.querySelector(`[data-runner="${name}"]`) as HTMLElement)
    expect(cell('cloud-ec2-2-agent-eva').getByTestId('runner-unit').textContent).toBe('· unit agent:eva')
    expect(cell('cloud-ec2-2-dimagi').getByTestId('runner-machine').textContent).toBe('· on ec2-2-role')
    expect(cell('cloud-ec2-9').queryByTestId('runner-machine')).toBeNull()
    expect(cell('jj-mbp').queryByTestId('runner-unit')).toBeNull()
    expect(screen.getAllByTestId('runner-machine')).toHaveLength(2)
  })
})
