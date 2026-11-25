// A project's files: the git repository canopy hosts for the project
// (`apps/agents/project_files.py`) — its skills, the scripts they run and their docs.
// Agents and editors draft changes (`PUT …/files/draft`, an MCP tool like every route);
// what is published runs in the project's member agents' turns with its vault, so only
// its lead, an admin on it or a workspace owner publishes, here, after reading the draft.
import { useCallback, useEffect, useState, type JSX } from 'react'
import { Button } from 'canopy-ui/ui'

import {
  discardProjectFiles,
  getProjectFile,
  getProjectFiles,
  publishProjectFiles,
  type ProjectFiles as ProjectFilesData,
  type ProjectFilesRevision,
} from '@/api/work'
import { Section } from '@/components/objectPage/settingsLayout'
import { Td, Th, Tr } from '@/components/DataTable'

const CHANGE_TONE: Record<string, string> = {
  added: 'text-success',
  modified: 'text-warning',
  removed: 'text-destructive',
}

function FileText({ workspace, handle, path, revision }: {
  workspace: string
  handle: string
  path: string
  revision: number
}): JSX.Element {
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  return (
    <details
      className="mt-0.5"
      onToggle={(e) => {
        if ((e.target as HTMLDetailsElement).open && text === null) {
          getProjectFile(workspace, handle, path, revision).then(setText, (err: unknown) =>
            setError(err instanceof Error ? err.message : 'Could not read it'),
          )
        }
      }}
    >
      <summary className="cursor-pointer break-all font-mono text-[12px] text-foreground">{path}</summary>
      {error && <div className="text-[12px] text-destructive">{error}</div>}
      {text !== null && (
        <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap rounded bg-muted p-2 font-mono text-[11.5px] text-foreground-secondary">
          {text}
        </pre>
      )}
    </details>
  )
}

function byWhom(rev: ProjectFilesRevision): string {
  return rev.author_agent ? `${rev.author_agent} (agent)` : rev.author || 'someone'
}

export function ProjectFiles({ workspace, handle }: { workspace: string; handle: string }): JSX.Element | null {
  const [data, setData] = useState<ProjectFilesData | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    getProjectFiles(workspace, handle).then(setData, (err: unknown) =>
      setError(err instanceof Error ? err.message : 'Could not load the files'),
    )
  }, [workspace, handle])
  useEffect(load, [load])

  if (!data) return error ? <div className="text-[13px] text-destructive">{error}</div> : null
  const { live, draft } = data
  if (!live && !draft && !data.may_draft) return null

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'That did not work')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section
      id="project-files"
      title="Files"
      description="Skills, the scripts they run, and their docs, kept in a git repository canopy hosts for this project. What is published runs in its agents' turns, with its vault."
    >
      {draft && (
        <div className="mb-3 rounded-lg border border-warning bg-card px-3 py-2 text-[13px]" data-testid="project-files-draft">
          <div className="text-foreground">
            <span className="font-medium">Draft {draft.number}</span> by {byWhom(draft)}
            {draft.message && <span className="text-muted-foreground"> — {draft.message}</span>}
          </div>
          <ul className="mt-1.5 space-y-0.5">
            {draft.changes.map((c) => (
              <li key={c.path} className="flex gap-2">
                <span className={`w-16 shrink-0 text-[12px] ${CHANGE_TONE[c.change] ?? 'text-muted-foreground'}`}>{c.change}</span>
                {c.change === 'removed' ? (
                  <span className="break-all font-mono text-[12px] text-muted-foreground line-through">{c.path}</span>
                ) : (
                  <FileText workspace={workspace} handle={handle} path={c.path} revision={draft.number} />
                )}
              </li>
            ))}
          </ul>
          <div className="mt-2 flex items-center gap-2">
            {data.may_publish ? (
              <Button size="sm" disabled={busy}
                onClick={() => void act(() => publishProjectFiles(workspace, handle, draft.number, draft.root_sha256))}>
                Publish
              </Button>
            ) : (
              <span className="text-[12px] text-muted-foreground">
                Waiting for the project&apos;s lead, an admin on it, or a workspace owner to publish it.
              </span>
            )}
            <Button size="sm" variant="ghost" disabled={busy}
              onClick={() => void act(() => discardProjectFiles(workspace, handle, draft.number))}>
              Discard
            </Button>
          </div>
        </div>
      )}
      {live ? (
        <div className="overflow-x-auto rounded-lg border border-border bg-card" data-testid="project-files-live">
          <table className="w-full text-[13px]">
            <thead>
              <Tr rule="bottom" head="label">
                <Th density="snug">Revision {live.number}{live.published_by ? `, published by ${live.published_by}` : ''}</Th>
                <Th density="snug" align="right">Size</Th>
              </Tr>
            </thead>
            <tbody>
              {live.files.map((f) => (
                <Tr rule="bottom-last" top key={f.path}>
                  <Td density="roomy">
                    <FileText workspace={workspace} handle={handle} path={f.path} revision={live.number} />
                  </Td>
                  <Td density="roomy" tone="muted" align="right" nowrap>{f.size.toLocaleString()} B</Td>
                </Tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-[13px] text-muted-foreground">
          Nothing published yet. An agent working on this project, or anyone who may change it, drafts files
          ({data.policy.join(', ')}); they appear here to publish.
        </p>
      )}
      {error && <div className="mt-2 text-[13px] text-destructive">{error}</div>}
    </Section>
  )
}
