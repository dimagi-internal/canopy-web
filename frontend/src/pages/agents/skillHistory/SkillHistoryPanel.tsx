import type { Panel, Selection } from './model'

type Crumb = { label: string; sel: Selection | null }

function Row({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return <button type="button" onClick={onClick} className="flex w-full gap-2.5 rounded-lg px-2 py-1.5 text-left hover:bg-muted">{children}</button>
}
const Kicker = ({ children }: { children: React.ReactNode }) => (
  <div className="font-mono text-[11.5px] uppercase tracking-wider text-muted-foreground">{children}</div>
)
const Stat = ({ value, label }: { value: React.ReactNode; label: string }) => (
  <div className="flex flex-col"><span className="text-[24px] font-semibold text-foreground">{value}</span><span className="text-[12px] text-muted-foreground">{label}</span></div>
)
const Change = ({ c }: { c: string }) => (
  <span className={`font-mono text-[11px] ${c.startsWith('+') || c === 'created' ? 'text-success' : c === 'removed' ? 'text-muted-foreground' : 'text-destructive'}`}>{c}</span>
)

export function SkillHistoryPanel({ panel, crumbs, dateLabel, onSelect, onDay, commitDay }: {
  panel: Panel; crumbs: Crumb[]; dateLabel: string
  // Returns null when the sha doesn't resolve to a known commit, so the
  // caller can leave "Move timeline" unrendered rather than falling back to
  // day 0 for an unknown commit.
  onSelect: (s: Selection) => void; onDay: (day: number) => void; commitDay: (sha: string) => number | null
}) {
  const moveDay = panel.kind === 'commit' ? commitDay(panel.sha) : null
  return (
    <aside className="flex max-h-[calc(100vh-48px)] w-full flex-col overflow-y-auto rounded-2xl border border-border bg-card lg:sticky lg:top-6 lg:w-[416px] lg:shrink-0">
      <nav aria-label="Selection" className="flex flex-wrap items-center gap-2 border-b border-border px-5 py-3.5 text-[13px]">
        {crumbs.map((c, i) => (
          <span key={i} className="flex items-center gap-2">
            {i < crumbs.length - 1
              ? <button type="button" className="text-primary underline underline-offset-2" onClick={() => onSelect(c.sel ?? {})}>{c.label}</button>
              : <span className="font-semibold text-foreground">{c.label}</span>}
            {i < crumbs.length - 1 && <span className="text-muted-foreground">›</span>}
          </span>
        ))}
      </nav>
      <div className="flex flex-col gap-5 p-5">
        {panel.kind === 'all' && <>
          <div><Kicker>7 days to {dateLabel}</Kicker>
            <div className="text-[20px] font-semibold text-foreground">{panel.week.length} commits</div></div>
          <div className="flex flex-col">
            {panel.week.length === 0 && <p className="text-[14px] text-muted-foreground">No skill revisions in these 7 days.</p>}
            {panel.week.map((c) => (
              <div key={c.sha} className="flex flex-col gap-1.5 border-b border-border py-2">
                <Row onClick={() => onSelect({ commit: c.sha })}>
                  <span className="w-11 shrink-0 font-mono text-[11.5px] text-muted-foreground">{c.date}</span>
                  <span className="line-clamp-2 text-[14px] text-foreground">{c.subject}</span>
                </Row>
                <div className="flex flex-wrap gap-1.5 pl-14">
                  {c.skills.map((s) => (
                    <button key={s} type="button" onClick={() => onSelect({ skill: s })}
                            className="rounded-md border border-border bg-background px-1.5 py-0.5 font-mono text-[11.5px] hover:border-primary">{s}</button>
                  ))}
                </div>
              </div>
            ))}
          </div>
          <div className="flex flex-col gap-1"><Kicker>Most revised, as of {dateLabel}</Kicker>
            {panel.top.map((t) => (
              <Row key={t.name} onClick={() => onSelect({ skill: t.name })}>
                <span className="flex-grow font-mono text-[12px]">{t.name}</span>
                <span className="font-mono text-[12px] text-muted-foreground">{t.revisions}</span>
              </Row>
            ))}
          </div>
        </>}

        {panel.kind === 'group' && <>
          <div><Kicker>{panel.kindLabel}</Kicker><div className="text-[24px] font-semibold text-foreground">{panel.title}</div></div>
          <div className="grid grid-cols-3 gap-3">
            <Stat value={panel.skills} label="skills" /><Stat value={panel.revisions} label="revisions incl. QA / eval" />
            <Stat value={panel.first ?? '—'} label="first revision" />
          </div>
          <div className="flex flex-col">
            {panel.rows.map((r) => (
              <Row key={r.name} onClick={() => onSelect({ skill: r.name })}>
                <span className="flex min-w-0 flex-grow flex-col">
                  <span className="truncate font-mono text-[12.5px] text-foreground">{r.name}</span>
                  {r.checkedBy.length > 0 && <span className="text-[11.5px] text-info">checked by {r.checkedBy.join(', ')}</span>}
                </span>
                <span className="w-12 font-mono text-[11.5px] text-muted-foreground">{r.first ?? '—'}</span>
                <span className="w-10 text-right font-mono text-[12px]">{r.revisions || '—'}</span>
                <span className="w-12 text-right font-mono text-[12px] text-muted-foreground">{r.lines?.toLocaleString() ?? '—'}</span>
              </Row>
            ))}
          </div>
          <div className="flex flex-col"><Kicker>Latest commits, as of {dateLabel}</Kicker>
            {panel.recent.map((c) => (
              <Row key={c.sha} onClick={() => onSelect({ commit: c.sha })}>
                <span className="w-11 shrink-0 font-mono text-[11.5px] text-muted-foreground">{c.date}</span>
                <span className="line-clamp-2 text-[13.5px]">{c.subject}</span>
              </Row>
            ))}
          </div>
        </>}

        {panel.kind === 'skill' && <>
          <div className="flex flex-col gap-1">
            {panel.group && <button type="button" className="self-start font-mono text-[11.5px] text-primary underline" onClick={() => onSelect({ group: panel.group! })}>{panel.group}</button>}
            <div className="break-words font-mono text-[20px] text-foreground">{panel.name}</div>
            <div className="text-[13px] text-foreground-secondary">
              Created {panel.created}{panel.removed ? ` · removed ${panel.removed}` : panel.lastRevised ? ` · last revised ${panel.lastRevised}` : ''}.
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <Stat value={panel.revisions} label="revisions" /><Stat value={panel.lines?.toLocaleString() ?? '—'} label="lines now" />
            <Stat value={panel.firstLines.toLocaleString()} label="lines at first" />
          </div>
          {(panel.checks || panel.checkedBy.length > 0) && (
            <div className="flex flex-col gap-1.5"><span className="text-[12px] text-muted-foreground">{panel.checks ? 'Checks' : 'Checked by'}</span>
              <div className="flex flex-wrap gap-1.5">
                {(panel.checks ? [panel.checks] : panel.checkedBy).map((s) => (
                  <button key={s} type="button" onClick={() => onSelect({ skill: s })}
                          className="rounded-md border border-border px-1.5 py-0.5 font-mono text-[11.5px] text-info hover:border-primary">{s}</button>
                ))}
              </div>
            </div>
          )}
          <div className="flex flex-col"><Kicker>Revisions, newest first</Kicker>
            {panel.list.map((r) => (
              <Row key={r.sha} onClick={() => onSelect({ commit: r.sha })}>
                <span className={`flex w-12 shrink-0 flex-col ${r.later ? 'opacity-40' : ''}`}>
                  <span className="font-mono text-[11.5px] text-muted-foreground">{r.date}</span><Change c={r.change} />
                </span>
                <span className={`line-clamp-2 text-[13.5px] ${r.later ? 'opacity-40' : ''}`}>{r.subject}</span>
              </Row>
            ))}
          </div>
        </>}

        {panel.kind === 'commit' && <>
          <div className="flex flex-col gap-2">
            <div className="font-mono text-[12px] text-muted-foreground">commit {panel.sha.slice(0, 8)} · {panel.date}</div>
            <div className="text-[17px] font-medium text-foreground">{panel.subject}</div>
          </div>
          {moveDay !== null && (
            <button type="button" onClick={() => onDay(moveDay)}
                    className="self-start rounded-lg border border-border px-3.5 py-2 text-[13.5px] hover:border-primary">Move timeline to {panel.date}</button>
          )}
          <div className="flex flex-col"><Kicker>Skills changed ({panel.rows.length})</Kicker>
            {panel.rows.map((r) => (
              <Row key={r.name} onClick={() => onSelect({ skill: r.name })}>
                <span className="flex-grow truncate font-mono text-[12.5px]">{r.name}</span>
                <Change c={r.change} />
                <span className="w-12 text-right font-mono text-[12px] text-muted-foreground">{r.lines ? r.lines.toLocaleString() : '—'}</span>
              </Row>
            ))}
          </div>
        </>}
      </div>
    </aside>
  )
}
