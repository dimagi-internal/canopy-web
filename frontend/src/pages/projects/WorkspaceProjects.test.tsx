// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const { ProjectMcpInstall, WorkspaceProjectLine } = await import('./WorkspaceProjects')

afterEach(() => {
  cleanup()
})

const kb = {
  id: 1, agent_slug: 'eva', workspace: 'dimagi', scope: 'workspace', handle: 'givewell-kb', steward_slug: 'eva',
  address: 'dimagi/givewell-kb', url: '/w/dimagi/projects/givewell-kb',
  board_url: '/w/dimagi/agents/eva/projects/P12', ext_id: 'P12', name: 'GiveWell Knowledge Base',
  outcome: 'Everything GiveWell publishes, cited', status: 'active', task_count: 3, open_task_count: 1,
} as never

describe('WorkspaceProjectLine', () => {
  it('names the project by its workspace address', () => {
    render(<WorkspaceProjectLine project={kb} />)
    expect(screen.getByTestId('workspace-project-line').textContent).toContain('dimagi/givewell-kb')
  })
})

describe('ProjectMcpInstall', () => {
  it('gives the one line to add its server, named by its handle', () => {
    render(<ProjectMcpInstall project={{ ...(kb as object), mcp_path: '/api/mcp/projects/connect/givewell-kb/' } as never} />)
    expect(screen.getByTestId('project-mcp-claude-code').textContent).toBe(
      `claude mcp add --transport http givewell-kb ${window.location.origin}/api/mcp/projects/connect/givewell-kb`,
    )
  })

  it('shows nothing for a project that serves nothing', () => {
    const { container } = render(<ProjectMcpInstall project={kb} />)
    expect(container.textContent).toBe('')
  })
})
