// A project's toolbox (`ProjectService`): its knowledge base, and the skills, MCP
// servers and plugins it shares. A project is the workspace's and spans its agents
// and people, so this is where they share tools. One canopy connection reaches
// every project's toolbox through the same few tools (`apps/mcp/project_gateway.py`),
// so nothing here reaches anyone's Claude until the work asks for it; the section
// says how each one reaches Claude and gives the one line that connects Claude Code.
import { useState, type FormEvent, type JSX } from 'react'
import { Button, CopyButton } from 'canopy-ui/ui'

import {
  addProjectService,
  removeProjectService,
  type ProjectDetail,
  type ProjectService,
  type ProjectServiceKind,
} from '@/api/work'
import { Link } from 'react-router-dom'
import { Section } from '@/components/objectPage/settingsLayout'
import { vaultHref } from '@/lib/scopedLinks'

const KIND_LABEL: Record<string, string> = {
  kb: 'Knowledge base',
  mcp: 'MCP server',
  skill: 'Skill',
  plugin: 'Plugin',
}

const FIELD = 'rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder'

/** The one line that gives Claude Code every project's toolbox (the canopy plugin already has it). */
export function connectCommand(origin: string): string {
  return `claude mcp add --transport http canopy-projects ${origin}/api/mcp/projects/`
}

function Command({ text }: { text: string }): JSX.Element {
  return (
    <span className="inline-flex items-center gap-2">
      <code className="break-all rounded bg-muted px-1.5 py-0.5 font-mono text-[11.5px] text-foreground-secondary">{text}</code>
      <CopyButton text={text} variant="ghost" />
    </span>
  )
}

function HowItLoads({ s }: { s: ProjectService }): JSX.Element {
  if (s.kind === 'kb') {
    return (
      <span className="text-[12px] text-muted-foreground">
        {s.url ? 'Searched when the work needs it' : 'Not published yet'}
      </span>
    )
  }
  if (s.kind === 'skill') {
    return <span className="text-[12px] text-muted-foreground">Read when the work needs it</span>
  }
  return s.add_command ? <Command text={s.add_command} /> : <span className="text-[12px] text-muted-foreground">—</span>
}

function HowToUse({ address }: { address: string }): JSX.Element {
  const origin = typeof window === 'undefined' ? '' : window.location.origin
  return (
    <div className="mb-3 rounded-lg border border-border bg-card px-3 py-2 text-[13px] text-foreground" data-testid="toolbox-how">
      <p className="text-muted-foreground">
        Nothing here reaches anyone&apos;s Claude until the work asks for it, so a project costs nothing in sessions that don&apos;t use it.
      </p>
      <ul className="mt-1.5 list-disc space-y-1 pl-5">
        <li>
          <span className="font-medium">Agents</span> are told to open it when they work a task in this project.
        </li>
        <li>
          <span className="font-medium">You, in Claude Code:</span> ask Claude to{' '}
          <span className="font-medium">&ldquo;open {address}&rdquo;</span>. The canopy plugin is already connected;
          without it, connect once for every project: <Command text={connectCommand(origin)} />
        </li>
        <li>
          Skills are read only when the work needs them. canopy does not load shared MCP servers or plugins; each has
          the line that adds it.
        </li>
      </ul>
    </div>
  )
}

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
  const vaults = detail.vaults ?? []
  const [kind, setKind] = useState<ProjectServiceKind>('skill')
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [description, setDescription] = useState('')
  const [instructions, setInstructions] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (!services.length && !vaults.length && !detail.may_edit) return null

  const isSkill = kind === 'skill'
  const ready = name.trim() && (isSkill ? instructions.trim() || url.trim() : url.trim())

  const add = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await addProjectService(workspace, handle, {
        kind,
        name: name.trim(),
        url: url.trim(),
        description,
        ...(isSkill ? { instructions } : {}),
      })
      setName('')
      setUrl('')
      setDescription('')
      setInstructions('')
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add it')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section
      id="project-services"
      title="Toolbox"
      description="What this project gives the people and agents working on it: its knowledge base, skills, and the MCP servers and plugins it shares."
    >
      <HowToUse address={`${workspace}/${handle}`} />
      {services.length > 0 && (
        <div className="mb-3 overflow-x-auto rounded-lg border border-border bg-card" data-testid="project-services">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                <th className="px-3 py-1.5 font-medium">Kind</th>
                <th className="px-3 py-1.5 font-medium">Name</th>
                <th className="px-3 py-1.5 font-medium">How it reaches Claude</th>
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
                    {s.kind === 'skill' && s.instructions && (
                      <details className="mt-1">
                        <summary className="cursor-pointer text-[12px] text-primary">Instructions</summary>
                        <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted p-2 font-mono text-[11.5px] text-foreground-secondary">
                          {s.instructions}
                        </pre>
                      </details>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <HowItLoads s={s} />
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
                            setError(err instanceof Error ? err.message : 'Could not remove it')
                          }
                        }}
                      >
                        Remove
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {vaults.length > 0 && (
        <div className="mb-3 rounded-lg border border-border bg-card px-3 py-2 text-[13px]" data-testid="project-vaults">
          <div className="font-medium text-foreground">Vaults</div>
          <p className="text-[12px] text-muted-foreground">
            Secrets this project&apos;s skills may need. Each member agent in the vault&apos;s workspace tree reads them in its
            turns; joining the project gives them, leaving takes them away. Names only.
          </p>
          <ul className="mt-1 space-y-1">
            {vaults.map((v) => (
              <li key={v.vault_id} data-testid={`project-vault-${v.vault_id}`}>
                <Link to={vaultHref(v.workspace, v.vault_id)} className="text-primary hover:underline">{v.name}</Link>
                <span className="text-muted-foreground">
                  {' '}— {v.secrets.length ? v.secrets.join(', ') : 'no secrets yet'}; serves{' '}
                  {v.served.length ? v.served.join(', ') : 'no agent'}
                  {v.project_outside_tree
                    ? '; the project is outside the vault’s workspace tree, so it serves nobody'
                    : v.not_served.length ? `; not served (outside the vault’s tree): ${v.not_served.join(', ')}` : ''}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {detail.may_edit && (
        <form onSubmit={add} className="space-y-2" data-testid="add-service">
          <div className="flex flex-wrap items-center gap-2">
            <select value={kind} onChange={(e) => setKind(e.target.value as ProjectServiceKind)} aria-label="Kind" className={FIELD}>
              <option value="skill">Skill</option>
              <option value="mcp">MCP server</option>
              <option value="plugin">Plugin</option>
            </select>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={isSkill ? 'Name, e.g. cite-givewell' : 'Name, e.g. salesforce'}
              aria-label="Name"
              className={`${FIELD} w-44`}
            />
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder={isSkill ? 'Where it came from (optional)' : 'https://…'}
              aria-label="URL"
              className={`${FIELD} min-w-56 flex-1`}
            />
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={isSkill ? 'When to use it' : 'What it is for (optional)'}
              aria-label="Description"
              className={`${FIELD} min-w-48 flex-1`}
            />
          </div>
          {isSkill && (
            <textarea
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
              placeholder="The instructions an agent follows when it uses this skill"
              aria-label="Instructions"
              rows={4}
              className={`${FIELD} w-full font-mono`}
            />
          )}
          <Button size="sm" type="submit" disabled={busy || !ready}>
            Add to toolbox
          </Button>
        </form>
      )}
      {error && <p role="alert" className="mt-2 text-[12px] text-destructive">{error}</p>}
    </Section>
  )
}
