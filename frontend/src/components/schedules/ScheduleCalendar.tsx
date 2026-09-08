import { useCallback, useEffect, useMemo, useState } from "react";
import type { Schedule, ScheduleWeekItem } from "@/api/schedules";
import { getScheduleWeek } from "@/api/schedules";
import { ScheduleEditor } from "@/components/agents/ScheduleEditor";
import { bucketByDay, matchesFilters, type Filters } from "./calendarGrid";

const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Monday 00:00 local of the week containing `d`. */
function mondayOf(d: Date): Date {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  const dow = (x.getDay() + 6) % 7; // Mon=0
  x.setDate(x.getDate() - dow);
  return x;
}

function timeLabel(d: Date): string {
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** Reads no props: which workspaces it spans is decided by the ROUTE it's on
 * (flat /schedules → all; /w/:ws/schedules → that one), handled transparently
 * by the api client's tenant-rewrite middleware. */
export function ScheduleCalendar({ showWorkspaceFilter }: { showWorkspaceFilter: boolean }) {
  const [weekStart, setWeekStart] = useState(() => mondayOf(new Date()));
  const [items, setItems] = useState<ScheduleWeekItem[] | null>(null);
  const [error, setError] = useState("");
  const [filters, setFilters] = useState<Filters>({ agent: null, workspace: null });
  const [editing, setEditing] = useState<{ agentSlug: string; schedule: Schedule } | null>(null);

  const load = useCallback(async () => {
    setItems(null);
    try {
      setItems(await getScheduleWeek(weekStart.toISOString()));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
      setItems([]);
    }
  }, [weekStart]);

  useEffect(() => { void load(); }, [load]);

  const shown = useMemo(() => (items ?? []).filter((i) => matchesFilters(i, filters)), [items, filters]);
  const columns = useMemo(() => bucketByDay(shown, weekStart), [shown, weekStart]);

  const agents = useMemo(
    () => [...new Set((items ?? []).map((i) => i.schedule.agent_slug))].sort(),
    [items],
  );
  const workspaces = useMemo(
    () => [...new Set((items ?? []).map((i) => i.workspace_slug).filter(Boolean))].sort() as string[],
    [items],
  );

  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;

  function shiftWeek(days: number) {
    const d = new Date(weekStart);
    d.setDate(d.getDate() + days);
    setWeekStart(mondayOf(d));
  }

  return (
    <div className="p-6">
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">Schedule</h1>
          <p className="text-xs text-muted-foreground">
            Week of {weekStart.toLocaleDateString(undefined, { month: "short", day: "numeric" })} · times in {tz}
          </p>
        </div>
        <div className="flex items-center gap-1">
          <button type="button" onClick={() => shiftWeek(-7)}
            className="min-h-11 min-w-11 rounded border border-border px-2 py-1 text-sm text-foreground hover:bg-muted sm:min-h-0 sm:min-w-0">←</button>
          <button type="button" onClick={() => setWeekStart(mondayOf(new Date()))}
            className="min-h-11 min-w-11 rounded border border-border px-2 py-1 text-sm text-foreground hover:bg-muted sm:min-h-0 sm:min-w-0">This week</button>
          <button type="button" onClick={() => shiftWeek(7)}
            className="min-h-11 min-w-11 rounded border border-border px-2 py-1 text-sm text-foreground hover:bg-muted sm:min-h-0 sm:min-w-0">→</button>
        </div>
      </header>

      <div className="mb-3 flex flex-wrap gap-2 text-xs">
        <FilterRow label="Agent" value={filters.agent} options={agents}
          onChange={(v) => setFilters((f) => ({ ...f, agent: v }))} />
        {showWorkspaceFilter && (
          <FilterRow label="Workspace" value={filters.workspace} options={workspaces}
            onChange={(v) => setFilters((f) => ({ ...f, workspace: v }))} />
        )}
      </div>

      {error && <p className="mb-3 text-sm text-destructive">{error}</p>}
      {items === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-7">
          {columns.map((col) => (
            <div key={col.index} className="rounded border border-border bg-card">
              <div className="border-b border-border px-2 py-1 text-xs font-medium text-muted-foreground">
                {DAY_LABELS[col.index]} {col.date.getDate()}
              </div>
              <ul className="min-h-16 space-y-1 p-1">
                {col.fires.map((f, i) => (
                  <li key={i}>
                    <button type="button"
                      onClick={() => setEditing({ agentSlug: f.item.schedule.agent_slug, schedule: f.item.schedule })}
                      className="min-h-11 w-full rounded bg-muted px-1.5 py-1 text-left text-xs hover:bg-primary/10 sm:min-h-0">
                      <span className="font-medium text-foreground">{timeLabel(f.when)}</span>{" "}
                      <span className="text-foreground-secondary">{f.item.schedule.agent_slug}</span>
                      <div className="truncate text-foreground-secondary">{f.item.schedule.name}</div>
                    </button>
                  </li>
                ))}
                {col.fires.length === 0 && <li className="px-1.5 py-1 text-xs text-foreground-subtle">—</li>}
              </ul>
            </div>
          ))}
        </div>
      )}

      {editing && (
        <ScheduleEditor
          agentSlug={editing.agentSlug}
          schedule={editing.schedule}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); void load(); }}
        />
      )}
    </div>
  );
}

function FilterRow({ label, value, options, onChange }: {
  label: string; value: string | null; options: string[]; onChange: (v: string | null) => void;
}) {
  if (options.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1">
      <span className="text-muted-foreground">{label}:</span>
      <button type="button" onClick={() => onChange(null)}
        className={`min-h-11 rounded border px-2 py-0.5 sm:min-h-0 ${value === null ? "border-primary text-primary" : "border-border text-foreground-secondary hover:bg-muted"}`}>
        All
      </button>
      {options.map((o) => (
        <button key={o} type="button" onClick={() => onChange(o)}
          className={`min-h-11 rounded border px-2 py-0.5 sm:min-h-0 ${value === o ? "border-primary text-primary" : "border-border text-foreground-secondary hover:bg-muted"}`}>
          {o}
        </button>
      ))}
    </div>
  );
}
