// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const listMcpServers = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listMcpServers: (...a: unknown[]) => listMcpServers(...a),
}))

const { McpServersPage } = await import('./McpServersPage')

afterEach(() => {
  cleanup()
  listMcpServers.mockReset()
})

const URL = 'https://canopy.test/api/mcp/projects/connect/givewell-kb'
const GW = {
  name: 'givewell-kb', address: 'connect/givewell-kb', workspace: 'connect', workspace_name: 'Connect',
  title: 'GiveWell Knowledge Base', description: 'Everything GiveWell has published.', kind: 'kb',
  steward_slug: 'eva', url: URL, add_command: `claude mcp add --transport http givewell-kb ${URL}`,
  project_url: '/w/connect/projects/givewell-kb', published_at: '2026-10-20T00:00:00Z', bytes: 180_000_000,
}

const at = (ws: string) =>
  render(
    <MemoryRouter initialEntries={[`/w/${ws}/mcp`]}>
      <Routes>
        <Route path="/w/:workspace/mcp" element={<McpServersPage />} />
      </Routes>
    </MemoryRouter>,
  )

describe('McpServersPage', () => {
  it('lists the servers below the workspace, naming the one each lives in, and adds only on request', async () => {
    listMcpServers.mockResolvedValue([GW])
    at('dimagi')
    expect(await screen.findByText('GiveWell Knowledge Base')).toBeTruthy()
    expect(listMcpServers).toHaveBeenCalledWith('dimagi')
    expect(screen.getByText('Connect')).toBeTruthy() // a division's server says where it lives
    expect(screen.getByText(/Kept by eva/).textContent).toContain('180.0 MB')
    expect(screen.queryByTestId('mcp-add-givewell-kb')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Add it' }))
    expect(screen.getByTestId('mcp-add-givewell-kb').textContent).toContain(GW.add_command)
  })

  it('does not label a server with the workspace you are already in', async () => {
    listMcpServers.mockResolvedValue([GW])
    at('connect')
    await screen.findByText('GiveWell Knowledge Base')
    expect(screen.queryByText('Connect')).toBeNull()
  })

  it('says when there is nothing to add', async () => {
    listMcpServers.mockResolvedValue([])
    at('acme')
    expect(await screen.findByText(/No project here serves an MCP server yet/)).toBeTruthy()
  })
})
