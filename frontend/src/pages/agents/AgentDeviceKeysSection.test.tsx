// @vitest-environment jsdom
//
// The machines that can read an agent's secrets: the list, and nothing to tick. A machine
// you approve can read; one you don't, can't — there is no separate "require approval" switch.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const { getAgentDeviceKeys } = vi.hoisted(() => ({ getAgentDeviceKeys: vi.fn() }))
vi.mock('@/api/deviceKeys', () => ({ getAgentDeviceKeys }))
vi.mock('@/components/settings/DeviceKeysTable', () => ({ DeviceKeysTable: () => <div data-testid="keys-table" /> }))

const { AgentDeviceKeysSection } = await import('./AgentDeviceKeysSection')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentDeviceKeysSection', () => {
  it('lists the machines, with no switch to tick', async () => {
    getAgentDeviceKeys.mockResolvedValue([{ id: 'k1', status: 'active' }])
    render(<AgentDeviceKeysSection slug="echo" canEdit />)
    expect(await screen.findByTestId('keys-table')).toBeTruthy()
    expect(screen.getByText('Machines that can read these secrets')).toBeTruthy()
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.queryByTestId('no-active-device-key')).toBeNull()
  })

  it('says so when no machine is approved, since then none can read', async () => {
    getAgentDeviceKeys.mockResolvedValue([{ id: 'k1', status: 'pending' }])
    render(<AgentDeviceKeysSection slug="echo" canEdit />)
    expect((await screen.findByTestId('no-active-device-key')).textContent).toContain('No approved machine yet')
  })

  it('is for admins only', () => {
    render(<AgentDeviceKeysSection slug="echo" canEdit={false} />)
    expect(screen.queryByTestId('agent-device-keys')).toBeNull()
    expect(getAgentDeviceKeys).not.toHaveBeenCalled()
  })
})
