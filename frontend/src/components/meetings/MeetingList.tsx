import { type JSX } from 'react'
import { Link } from 'react-router-dom'
import { KIND_LABELS, type MeetingRow } from '@/api/meetings'
import { useScopedPath } from '@/lib/scopedLinks'

function when(row: MeetingRow): string {
  return new Date(row.started_at ?? row.created_at).toLocaleDateString(undefined, { dateStyle: 'medium' })
}

function kinds(row: MeetingRow): string {
  return [...new Set(row.has.map((h) => KIND_LABELS[h.split(':')[0]] ?? h))].join(', ')
}

export function MeetingList({ rows, showProject = true }: { rows: readonly MeetingRow[]; showProject?: boolean }): JSX.Element {
  const scoped = useScopedPath()
  return (
    <ul className="divide-y divide-border rounded-lg border border-border bg-card" data-testid="meeting-list">
      {rows.map((row) => (
        <li key={row.id} className="px-4 py-3 text-[13px]">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <Link to={scoped(`/meetings/${row.id}`)} className="font-medium hover:underline">
              {row.title || 'Untitled meeting'}
            </Link>
            <span className="text-[12px] text-muted-foreground">{when(row)}</span>
          </div>
          <p className="mt-0.5 text-[12px] text-muted-foreground">
            {showProject && (row.project ? row.project.name : 'Just you')}
            {showProject && kinds(row) && ' · '}
            {kinds(row)}
            {!row.is_mine && ` · saved by ${row.owner}`}
          </p>
          {row.snippet && <p className="mt-1 text-[12px]">{row.snippet}</p>}
        </li>
      ))}
    </ul>
  )
}
