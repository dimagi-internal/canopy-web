import { useCallback, useEffect, useState } from 'react'

import { addAgentDefault, listAgentDefaults, removeAgentDefault, type AccessLineOut, type AgentDefaultsOut } from '@/api/agents'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { AccessLineForm } from './AccessLineForm'
import { lineWhat } from './accessWords'
import { Td, Tr } from '@/components/DataTable'

// DEFAULT ACCESS FOR AGENTS: lines every agent in this workspace and below inherits, unless
// that agent opts out (its Access setting). Workspace admins change them; a default that
// gives the whole agent (editor) is the workspace OWNER's call. The server says what the
// caller may do (`can_edit`, `can_give_write`) and refuses the rest in its own words.
export function WorkspaceAgentDefaults({ workspace }: { workspace: string }) {
  const { workspaces } = useWorkspace()
  const [data, setData] = useState<AgentDefaultsOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setData(await listAgentDefaults(workspace))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not load the default access')
    }
  }, [workspace])

  useEffect(() => {
    void load()
  }, [load])

  const remove = async (l: AccessLineOut) => {
    setError(null)
    try {
      await removeAgentDefault(workspace, l.id)
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not remove that')
    }
  }

  return (
    <div className="mb-8 rounded-lg border border-border bg-card p-5" data-testid="agent-defaults">
      <h2 className="mb-1 text-sm font-semibold text-foreground">Default access for agents</h2>
      <p className="mb-3 text-[12px] text-muted-foreground">
        These people get these roles on every agent in this workspace and below, unless an agent opts out. An
        agent can add its own on top.
      </p>

      {data === null ? (
        error ? null : <div className="h-12 animate-pulse rounded-lg bg-muted" />
      ) : data.lines.length === 0 ? (
        <p className="m-0 mb-3 text-[12px] text-muted-foreground">No defaults yet.</p>
      ) : (
        <table className="mb-3 w-full text-[12px]">
          <tbody>
            {data.lines.map((l) => (
              <Tr rule="bottom-soft" key={l.id} data-testid="agent-default">
                <Td density="compact-gap" tone="foreground">{l.who}</Td>
                <Td density="compact-gap" tone="foreground">{lineWhat(l, data?.roles)}</Td>
                <Td tone="muted">
                  {l.manual_only ? 'needs approval before anything goes out' : ''}
                </Td>
                <Td align="right">
                  {l.can_edit && (
                    <button
                      type="button"
                      onClick={() => void remove(l)}
                      aria-label={`Remove default for ${l.who}`}
                      className="rounded border border-input px-1.5 text-[11px] text-muted-foreground hover:border-destructive hover:text-destructive"
                    >
                      Remove
                    </button>
                  )}
                </Td>
              </Tr>
            ))}
          </tbody>
        </table>
      )}

      {error && (
        <p role="alert" className="m-0 mb-2 text-[12px] text-destructive">
          {error}
        </p>
      )}

      {data?.can_edit && (
        <AccessLineForm
          roles={data.roles}
          workspaces={workspaces.map((w) => [w.slug, w.display_name] as [string, string])}
          canGiveWrite={data.can_give_write}
          submitLabel="Add default"
          onAdd={async (line) => {
            const added = await addAgentDefault(workspace, line)
            setError(null)
            setNotice(added.warning || null)
            await load()
          }}
        />
      )}
      {notice && (
        <p role="status" className="m-0 mt-2 text-[12px] text-warning" data-testid="agent-default-warning">
          {notice}
        </p>
      )}
      {data?.can_edit && !data.can_give_write && (
        <p className="m-0 mt-2 text-[11px] text-muted-foreground">
          Only a workspace owner can give a role that drives or changes the agent to everyone as a default.
        </p>
      )}
    </div>
  )
}
