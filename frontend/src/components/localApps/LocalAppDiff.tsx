import type { JSX } from 'react'
import type { LocalAppFileDiff } from '@/api/localApps'

/** A longer file starts closed, so one big file doesn't push the rest of a review off the page. */
const OPEN_UP_TO_LINES = 200

const STATUS_WORD: Record<LocalAppFileDiff['status'], string> = { added: 'added', removed: 'removed', changed: 'changed' }

/**
 * The tone of one unified diff line. The first two lines are the `---`/`+++` file header; after
 * them the same characters are code, since a removed `-- comment` reads `--- comment`.
 */
export function diffLineKind(line: string, index: number): 'header' | 'hunk' | 'added' | 'removed' | 'context' {
  if (index < 2 && (line.startsWith('--- ') || line.startsWith('+++ '))) return 'header'
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+')) return 'added'
  if (line.startsWith('-')) return 'removed'
  return 'context'
}

const LINE_CLASS: Record<ReturnType<typeof diffLineKind>, string> = {
  header: 'text-muted-foreground',
  hunk: 'bg-muted text-muted-foreground',
  added: 'bg-success/10 text-success',
  removed: 'bg-destructive/10 text-destructive',
  context: 'text-foreground',
}

function FileDiff({ file }: { file: LocalAppFileDiff }): JSX.Element {
  const lines = file.lines
  return (
    <details open={lines.length <= OPEN_UP_TO_LINES} className="rounded-md border border-border bg-card" data-testid="local-app-file-diff">
      <summary className="cursor-pointer px-3 py-1.5 text-[13px]">
        <span className="font-mono text-foreground">{file.path}</span>
        <span className="ml-2 text-xs text-muted-foreground">
          {STATUS_WORD[file.status]} · {lines.length} {lines.length === 1 ? 'line' : 'lines'}
          {lines.length > OPEN_UP_TO_LINES && ', closed because it is long'}
        </span>
      </summary>
      {lines.length > 0 && (
        <div className="overflow-x-auto border-t border-border">
          <pre className="m-0 min-w-fit font-mono text-xs leading-5">
            {lines.map((line, i) => {
              const kind = diffLineKind(line, i)
              return <div key={i} data-line={kind} className={`whitespace-pre px-3 ${LINE_CLASS[kind]}`}>{line || ' '}</div>
            })}
          </pre>
        </div>
      )}
      {file.truncated && (
        <p className="m-0 border-t border-border px-3 py-1.5 text-xs text-warning" data-testid="local-app-diff-truncated">
          {lines.length > 0
            ? 'This diff stops here: it reached the limit on how much a review shows. The rest of the file is not shown.'
            : 'Not shown: the review reached its limit before this file.'}
        </p>
      )}
      {lines.length === 0 && !file.truncated && (
        <p className="m-0 border-t border-border px-3 py-1.5 text-xs text-muted-foreground">An empty file.</p>
      )}
    </details>
  )
}

/** A submission's per-file diff against the last release. */
export function LocalAppDiff({ files }: { files: LocalAppFileDiff[] }): JSX.Element {
  if (files.length === 0) return <p className="text-sm text-muted-foreground">No files changed.</p>
  return <div className="space-y-2">{files.map((f) => <FileDiff key={f.path} file={f} />)}</div>
}
