// What a project serves to its workspace (`ProjectService`): its knowledge base,
// and the MCP servers, skills and plugins it shares. A project is the
// workspace's and spans its agents and people, so this is where they share
// tools: everyone who can see the project sees these, with the line that adds
// each to Claude Code; its editors share and stop sharing them here.
import { useState, type FormEvent, type JSX } from 'react'
import { Button, CopyButton } from 'canopy-ui/ui'

import {
  addProjectService,
  removeProjectService,
  type ProjectDetail,
  type ProjectServiceKind,
} from '@/api/work'
import { Section } from '@/components/objectPage/settingsLayout'

const KIND_LABEL: Record<string, string> = {
  kb: 'Knowledge base',
  mcp: 'MCP server',
  skill: 'Skill',
  plugin: 'Plugin',
}

const FIELD = 'rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder'

export function ProjectServices({
  detail,
  workspace,
  handle,
  onChanged,
}: {
  detail: ProjectDetail
  workspace: string
  handle: string
  onChanged: () => void
}): JSX.Element | null {
  const services = detail.services ?? []
  const [kind, setKind] = useState<ProjectServiceKind>('mcp')
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (!services.length && !detail.may_edit) return null

  const add = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await addProjectService(workspace, handle, { kind, name: name.trim(), url: url.trim(), description })
      setName('')
      setUrl('')
      setDescription('')
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not share it')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section
      id="project-services"
      title="Shared with the workspace"
      description="Tools this project serves to everyone in the workspace: its knowledge base, and the MCP servers, skills and plugins it shares."
    >
      {services.length > 0 && (
        <div className="mb-3 overflow-x-auto rounded-lg border border-border bg-card" data-testid="project-services">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                <th className="px-3 py-1.5 font-medium">Kind</th>
                <th className="px-3 py-1.5 font-medium">Name</th>
                <th className="px-3 py-1.5 font-medium">Add it</th>
                <th className="px-3 py-1.5" />
              </tr>
            </thead>
            <tbody>
              {services.map((s) => (
                <tr key={s.id} className="border-b border-border last:border-0 align-top">
                  <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">{KIND_LABEL[s.kind] ?? s.kind}</td>
                  <td className="px-3 py-2">
                    <div className="font-medium text-foreground">{s.name}</div>
                    {s.description && <div className="text-[12px] text-muted-foreground">{s.description}</div>}
                    {s.url && s.kind !== 'kb' && (
                      <a href={s.url} target="_blank" rel="noreferrer" className="break-all text-[12px] text-primary hover:underline">
                        {s.url}
                      </a>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {s.add_command ? (
                      <span className="inline-flex items-center gap-2">
                        <code className="break-all rounded bg-muted px-1.5 py-0.5 font-mono text-[11.5px] text-foreground-secondary">
                          {s.add_command}
                        </code>
                        <CopyButton text={s.add_command} variant="ghost" />
                      </span>
                    ) : (
                      <span className="text-[12px] text-muted-foreground">{s.kind === 'kb' ? 'Not published yet' : '—'}</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {detail.may_edit && s.kind !== 'kb' && (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={async () => {
                          try {
                            await removeProjectService(workspace, handle, s.id)
                            onChanged()
                          } catch (err) {
                            setError(err instanceof Error ? err.message : 'Could not stop sharing it')
                          }
                        }}
                      >
                        Stop sharing
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {detail.may_edit && (
        <form onSubmit={add} className="flex flex-wrap items-center gap-2" data-testid="add-service">
          <select value={kind} onChange={(e) => setKind(e.target.value as ProjectServiceKind)} aria-label="Kind" className={FIELD}>
            <option value="mcp">MCP server</option>
            <option value="skill">Skill</option>
            <option value="plugin">Plugin</option>
          </select>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name, e.g. salesforce" aria-label="Name" className={`${FIELD} w-40`} />
          <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" aria-label="URL" className={`${FIELD} min-w-56 flex-1`} />
          <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What it is for (optional)" aria-label="Description" className={`${FIELD} min-w-48 flex-1`} />
          <Button size="sm" type="submit" disabled={busy || !name.trim() || !url.trim()}>
            Share
          </Button>
        </form>
      )}
      {error && <p role="alert" className="mt-2 text-[12px] text-destructive">{error}</p>}
    </Section>
  )
}
