import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import { Link, useOutletContext, useSearchParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'
import {
  discardRevision,
  getDefinition,
  getRevision,
  getRevisionDiff,
  getRevisionFile,
  listRevisions,
  publishRevision,
  rollbackTo,
  type DefinitionDiffOut,
  type DefinitionOut,
  type DefinitionRevisionOut,
  type DefinitionRevisionSummaryOut,
} from '@/api/agentDefinitions'
import { Td, Th, Tr } from '@/components/DataTable'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { scopedPath } from '@/lib/scopedLinks'

/**
 * A STORED agent's definition (docs/architecture/stored-agents.md): the files of its
 * live revision and of its one draft, the draft's changes against live, and every
 * revision. Publish and Rollback show only to the agent's owner and admins acting as
 * themselves (`can_publish`, the server's own answer); Discard to anyone who may draft.
 * The server enforces every rule; this page only offers what it says the viewer may do.
 */
export function AgentDefinitionSection(): JSX.Element {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [params, setParams] = useSearchParams()
  const [def, setDef] = useState<DefinitionOut | null>(null)
  const [revisions, setRevisions] = useState<DefinitionRevisionSummaryOut[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      const [d, revs] = await Promise.all([getDefinition(agent.slug), listRevisions(agent.slug)])
      setDef(d)
      setRevisions(revs)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not read the definition.')
    }
  }, [agent.slug])
  useEffect(() => void load(), [load])

  const selected = Number(params.get('revision')) || def?.draft?.number || def?.live?.number || 0
  const file = params.get('file') ?? ''
  const select = (next: { revision?: number; file?: string }) => {
    const p = new URLSearchParams(params)
    if (next.revision !== undefined) p.set('revision', String(next.revision))
    if (next.file !== undefined) {
      if (next.file) p.set('file', next.file)
      else p.delete('file')
    }
    setParams(p, { replace: true })
  }

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label)
    try {
      await fn()
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }

  if (def && def.source !== 'stored') {
    return (
      <div className="max-w-5xl px-6 py-8">
        <WorkbenchSubHeader title="Definition" />
        <p className="text-[13px] text-foreground-secondary">
          {agent.name}&apos;s definition is its git repository.
        </p>
      </div>
    )
  }

  const draft = def?.draft ?? null
  return (
    <div className="max-w-6xl px-6 py-8">
      <WorkbenchSubHeader title="Definition" />
      <p className="-mt-2 mb-4 text-[13px] text-foreground-secondary">
        Canopy keeps {agent.name}&apos;s files. Its runners run the live revision. A change is a draft, and it goes
        live only when an owner or admin of {agent.name} publishes it.
      </p>
      {error && (
        <p role="alert" className="mb-4 text-[13px] text-destructive">
          {error}
        </p>
      )}

      {def && (
        <table className="mb-6 w-full text-[13px]" data-testid="definition-summary">
          <tbody>
            <Tr rule="bottom">
              <Th>Live</Th>
              <Td>
                {def.live ? <RevisionLine rev={def.live} /> : 'none'}
              </Td>
            </Tr>
            <Tr rule="bottom">
              <Th>Draft</Th>
              <Td>
                {draft ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <RevisionLine rev={draft} />
                    {draft.approval_id && (
                      <Link className="text-primary hover:underline"
                        to={scopedPath(agent.workspace ?? '', `/approvals/${draft.approval_id}`)}>
                        Approval request
                      </Link>
                    )}
                    {def.can_publish && (
                      <Button size="sm" disabled={Boolean(busy)}
                        onClick={() => void act('publish', () => publishRevision(agent.slug, draft.number))}>
                        {busy === 'publish' ? 'Publishing…' : 'Publish'}
                      </Button>
                    )}
                    {def.can_draft && (
                      <Button size="sm" variant="outline" disabled={Boolean(busy)}
                        onClick={() => void act('discard', () => discardRevision(agent.slug, draft.number))}>
                        {busy === 'discard' ? 'Discarding…' : 'Discard'}
                      </Button>
                    )}
                  </div>
                ) : (
                  <span className="text-muted-foreground">
                    No draft. Propose one with <code>canopy agent definition edit</code>.
                  </span>
                )}
              </Td>
            </Tr>
          </tbody>
        </table>
      )}

      {draft && <DraftDiff slug={agent.slug} draft={draft.number} />}

      {selected > 0 && (
        <FileBrowser
          slug={agent.slug}
          revision={selected}
          file={file}
          onFile={(f) => select({ file: f })}
        />
      )}

      <h3 className="mb-2 mt-8 text-sm font-semibold text-foreground">Revisions</h3>
      <table className="w-full text-[13px]" data-testid="definition-revisions">
        <thead>
          <Tr rule="bottom" head="plain">
            <Th>#</Th>
            <Th>Status</Th>
            <Th>Message</Th>
            <Th>By</Th>
            <Th>When</Th>
            <Th>{''}</Th>
          </Tr>
        </thead>
        <tbody>
          {revisions.map((r) => (
            <Tr key={r.number} rule="bottom" aria-current={r.number === selected ? 'true' : undefined}>
              <Td>
                <button type="button" className="text-primary hover:underline"
                  onClick={() => select({ revision: r.number, file: '' })}>
                  {r.number}
                </button>
              </Td>
              <Td>{r.status}</Td>
              <Td title={r.message}>{r.message}</Td>
              <Td>{r.author.by_agent ? `${r.author.email} (agent)` : r.author.email}</Td>
              <Td tone="muted" nowrap>
                {new Date(r.published_at ?? r.created_at).toLocaleString()}
              </Td>
              <Td align="right">
                {def?.can_publish && r.status === 'superseded' && (
                  <Button size="sm" variant="outline" disabled={Boolean(busy)}
                    onClick={() => {
                      if (window.confirm(`Publish revision ${r.number}'s files again as a new revision?`)) {
                        void act('rollback', () => rollbackTo(agent.slug, r.number))
                      }
                    }}>
                    Roll back to this
                  </Button>
                )}
              </Td>
            </Tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RevisionLine({ rev }: { rev: DefinitionRevisionSummaryOut }): JSX.Element {
  return (
    <span>
      Revision {rev.number} · {rev.files} files · <code className="text-[11px]">{rev.root_sha256.slice(0, 12)}</code>
      {rev.message ? ` · ${rev.message}` : ''}
    </span>
  )
}

function DraftDiff({ slug, draft }: { slug: string; draft: number }): JSX.Element {
  const [diff, setDiff] = useState<DefinitionDiffOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    getRevisionDiff(slug, draft)
      .then((d) => {
        setDiff(d)
        setError(null)
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [slug, draft])
  return (
    <section className="mb-6" data-testid="definition-draft-diff">
      <h3 className="mb-2 text-sm font-semibold text-foreground">Draft changes against live</h3>
      {error && <p className="text-[13px] text-destructive">{error}</p>}
      {diff?.files.map((f) => (
        <details key={f.path} className="mb-2 rounded border border-border" open={diff.files.length <= 3}>
          <summary className="cursor-pointer px-2 py-1 text-[13px]">
            <span className="text-muted-foreground">{f.status}</span> {f.path}
            {f.truncated && <span className="text-muted-foreground"> (cut short)</span>}
          </summary>
          <pre className="overflow-x-auto px-2 py-1 text-[12px] leading-snug">
            {f.diff.split('\n').map((line, i) => (
              <div key={i} className={line.startsWith('+') ? 'text-success' : line.startsWith('-') ? 'text-destructive' : ''}>
                {line || ' '}
              </div>
            ))}
          </pre>
        </details>
      ))}
    </section>
  )
}

function FileBrowser({ slug, revision, file, onFile }: {
  slug: string
  revision: number
  file: string
  onFile: (path: string) => void
}): JSX.Element {
  const [rev, setRev] = useState<DefinitionRevisionOut | null>(null)
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    getRevision(slug, revision)
      .then((r) => {
        setRev(r)
        setError(null)
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [slug, revision])
  useEffect(() => {
    setText(null)
    if (!file) return
    getRevisionFile(slug, revision, file)
      .then(setText)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [slug, revision, file])
  const paths = useMemo(() => Object.keys(rev?.tree ?? {}).sort(), [rev])
  return (
    <section data-testid="definition-files">
      <h3 className="mb-2 text-sm font-semibold text-foreground">
        Files of revision {revision}{rev ? ` (${rev.status})` : ''}
      </h3>
      {error && <p className="text-[13px] text-destructive">{error}</p>}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-[minmax(0,16rem)_minmax(0,1fr)]">
        <ul className="text-[13px]">
          {paths.map((p) => (
            <li key={p}>
              <button type="button" onClick={() => onFile(p)}
                className={`w-full truncate text-left hover:text-primary ${p === file ? 'font-semibold text-primary' : ''}`}>
                {p}{rev?.tree[p]?.executable ? ' *' : ''}
              </button>
            </li>
          ))}
        </ul>
        <div className="min-w-0">
          {file ? (
            <pre className="overflow-x-auto rounded border border-border p-2 text-[12px] leading-snug"
              data-testid="definition-file-text">
              {text ?? 'Loading…'}
            </pre>
          ) : (
            <p className="text-[13px] text-muted-foreground">Choose a file to read it.</p>
          )}
        </div>
      </div>
    </section>
  )
}
