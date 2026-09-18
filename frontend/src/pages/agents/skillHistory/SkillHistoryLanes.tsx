import type { GroupRow, Selection } from './model'

const LOOK = {
  unborn: 'border border-dashed border-border bg-transparent opacity-50',
  active: 'border border-border bg-card',
  recent: 'border-2 border-primary bg-primary/5',
  removed: 'border border-border bg-muted opacity-70',
} as const

export function SkillHistoryLanes({ rows, onSelect }: { rows: GroupRow[]; onSelect: (s: Selection) => void }) {
  return (
    <section className="flex flex-col">
      <header className="flex flex-wrap items-baseline justify-between gap-3 border-b border-border pb-2">
        <h2 className="m-0 text-[18px] font-semibold text-foreground">Skills by agent</h2>
        <ul className="m-0 flex list-none flex-wrap gap-4 p-0 text-[12px] text-foreground-secondary">
          <li className="flex items-center gap-1.5"><span className="h-1.5 w-5 rounded bg-primary" />revisions</li>
          <li className="flex items-center gap-1.5"><span className="h-1 w-5 rounded bg-info" />its QA / eval skills</li>
          <li className="flex items-center gap-1.5"><span className="h-3 w-3 rounded border-2 border-primary" />revised in the last 7 days</li>
          <li className="flex items-center gap-1.5"><span className="h-3 w-3 rounded border border-dashed border-border" />not yet created</li>
        </ul>
      </header>
      {rows.map((g) => (
        <div key={g.index} className={`flex items-start gap-3 border-b border-border py-2.5 ${g.dimmed ? 'opacity-45' : ''}`}>
          <button type="button" aria-label={`Open ${g.title}`} onClick={() => onSelect({ group: g.title })}
                  className="w-[150px] shrink-0 rounded-md px-1.5 py-1 text-left hover:bg-muted">
            <span className="block font-mono text-[12px] text-primary">{g.kind === 'phase' ? `PHASE ${g.num}` : g.kind === 'agent' ? 'AGENT' : 'NO AGENT'}</span>
            <span className="block text-[14px] font-semibold leading-tight text-foreground">{g.title}</span>
            <span className="block text-[12px] text-muted-foreground">{g.meta}</span>
          </button>
          <div className="flex flex-grow flex-wrap gap-2">
            {g.tiles.map((t) => (
              <button key={t.name} type="button" aria-label={`Skill ${t.name}`} onClick={() => onSelect({ skill: t.name })}
                      title={`${t.name} · ${t.revisions} revisions${t.lines ? `, ${t.lines} lines` : ''}`}
                      className={`flex h-14 w-28 flex-col gap-1.5 rounded-lg px-2 py-1.5 text-left hover:ring-2 hover:ring-primary ${LOOK[t.state]} ${t.selected ? 'ring-[3px] ring-foreground' : ''}`}>
                <span className={`block w-full truncate font-mono text-[10.5px] ${t.state === 'removed' ? 'text-muted-foreground line-through' : 'text-foreground'}`}>{t.name}</span>
                <span className="flex items-center gap-1.5">
                  <span className="block h-[5px] rounded bg-primary" style={{ width: Math.min(t.revisions, 90) }} />
                  <span className="font-mono text-[10px] text-muted-foreground">{t.state === 'removed' ? 'removed' : t.revisions || ''}</span>
                </span>
                {t.checkRevisions > 0 && (
                  <span className="flex items-center gap-1.5">
                    <span className="block h-1 rounded bg-info" style={{ width: Math.min(t.checkRevisions, 90) }} />
                    <span className="font-mono text-[10px] text-info">{t.checkRevisions}</span>
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>
      ))}
    </section>
  )
}
