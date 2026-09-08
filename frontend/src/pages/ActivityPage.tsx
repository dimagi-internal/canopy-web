import { useCallback, useEffect, useMemo, useState } from "react";
import { listTurns, listTurnEvents, type TurnEvent } from "@/api/turns";
import {
  type Turn,
  type TurnFilters,
  agentLabel,
  originLabel,
  matchesTurnFilters,
  relativeTime,
  statusToken,
} from "@/components/activity/turnLog";

const LIMIT = 20;

/** Mounted at BOTH /activity (all my workspaces) and /w/:workspace/activity
 * (that workspace). Same component; the api client picks the scope from the
 * URL. Read-only log of the last 20 fired turns. Sibling of the Schedule page:
 * schedules are what WILL fire, this is what DID. */
export default function ActivityPage() {
  const [turns, setTurns] = useState<Turn[] | null>(null);
  const [error, setError] = useState("");
  const [filters, setFilters] = useState<TurnFilters>({ agent: null, origin: null, status: null });
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    setTurns(null);
    try {
      setTurns(await listTurns(LIMIT));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
      setTurns([]);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const shown = useMemo(
    () => (turns ?? []).filter((t) => matchesTurnFilters(t, filters)),
    [turns, filters],
  );
  const agents = useMemo(
    () => [...new Set((turns ?? []).map(agentLabel))].sort(),
    [turns],
  );
  const origins = useMemo(
    () => [...new Set((turns ?? []).map((t) => t.origin))].sort(),
    [turns],
  );
  const statuses = useMemo(
    () => [...new Set((turns ?? []).map((t) => t.status))].sort(),
    [turns],
  );

  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const now = new Date();

  return (
    <div className="p-6">
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">Activity</h1>
          <p className="text-xs text-muted-foreground">
            Last {LIMIT} triggered turns · times in {tz}
          </p>
        </div>
        <button type="button" onClick={() => void load()}
          className="rounded border border-border px-2 py-1 text-sm text-foreground hover:bg-muted">
          Refresh
        </button>
      </header>

      <div className="mb-3 flex flex-wrap gap-2 text-xs">
        <FilterRow label="Agent" value={filters.agent} options={agents}
          onChange={(v) => setFilters((f) => ({ ...f, agent: v }))} />
        <FilterRow label="Trigger" value={filters.origin} options={origins}
          onChange={(v) => setFilters((f) => ({ ...f, origin: v }))} />
        <FilterRow label="Status" value={filters.status} options={statuses}
          onChange={(v) => setFilters((f) => ({ ...f, status: v }))} />
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {!error && (turns === null ? (
        <SkeletonRows />
      ) : shown.length === 0 ? (
        <EmptyState hasTurns={(turns ?? []).length > 0} />
      ) : (
        <div className="overflow-x-auto rounded border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium">Agent</th>
                <th className="px-3 py-2 font-medium">Trigger</th>
                <th className="px-3 py-2 font-medium">Runner</th>
                <th className="px-3 py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((t) => (
                <TurnRow key={t.id} turn={t} now={now}
                  open={expanded === t.id}
                  onToggle={() => setExpanded((cur) => (cur === t.id ? null : t.id))} />
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

function TurnRow({ turn, now, open, onToggle }: {
  turn: Turn; now: Date; open: boolean; onToggle: () => void;
}) {
  return (
    <>
      <tr onClick={onToggle}
        className="cursor-pointer border-b border-border last:border-0 hover:bg-muted">
        <td className="px-3 py-2 text-foreground-secondary" title={new Date(turn.created_at).toLocaleString()}>
          {relativeTime(turn.created_at, now)}
        </td>
        <td className="px-3 py-2 text-foreground">{agentLabel(turn)}</td>
        <td className="px-3 py-2 text-muted-foreground">{originLabel(turn)}</td>
        <td className="px-3 py-2 text-foreground-secondary">{turn.claimed_by_name ?? "—"}</td>
        <td className="px-3 py-2">
          <span className={`inline-block rounded border px-1.5 py-0.5 text-xs ${statusToken(turn.status)}`}>
            {turn.status}
          </span>
        </td>
      </tr>
      {open && (
        <tr className="border-b border-border bg-card">
          <td colSpan={5} className="px-3 py-2">
            <EventLedger turnId={turn.id} />
          </td>
        </tr>
      )}
    </>
  );
}

/** Lazily loads the turn's event ledger on first expand; component unmounts on
 * collapse, so re-expanding refetches — fine for a rarely-opened drill-down. */
function EventLedger({ turnId }: { turnId: string }) {
  const [events, setEvents] = useState<TurnEvent[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    listTurnEvents(turnId)
      .then((e) => { if (alive) setEvents(e); })
      .catch((e) => { if (alive) setError(e instanceof Error ? e.message : "Failed to load events"); });
    return () => { alive = false; };
  }, [turnId]);

  if (error) return <p className="text-xs text-destructive">{error}</p>;
  if (events === null) return <p className="text-xs text-muted-foreground">Loading events…</p>;
  if (events.length === 0) return <p className="text-xs text-muted-foreground">No events recorded.</p>;

  return (
    <ol className="space-y-1">
      {events.map((e) => (
        <li key={e.seq} className="flex gap-2 text-xs">
          <span className="text-foreground-subtle tabular-nums">#{e.seq}</span>
          <span className="text-foreground-secondary" title={new Date(e.ts).toLocaleString()}>
            {new Date(e.ts).toLocaleTimeString()}
          </span>
          <span className="font-medium text-foreground">{e.kind}</span>
        </li>
      ))}
    </ol>
  );
}

function FilterRow({ label, value, options, onChange }: {
  label: string; value: string | null; options: string[]; onChange: (v: string | null) => void;
}) {
  return (
    <label className="flex items-center gap-1">
      <span className="text-muted-foreground">{label}</span>
      <select
        className="min-h-11 rounded border border-input bg-input px-1.5 py-1 text-foreground sm:min-h-0"
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value || null)}
      >
        <option value="">All</option>
        {options.map((o) => (<option key={o} value={o}>{o}</option>))}
      </select>
    </label>
  );
}

function SkeletonRows() {
  return (
    <div className="space-y-2" aria-busy="true">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="h-8 animate-pulse rounded bg-muted" />
      ))}
    </div>
  );
}

function EmptyState({ hasTurns }: { hasTurns: boolean }) {
  return (
    <p className="text-sm text-muted-foreground">
      {hasTurns
        ? "No turns match these filters."
        : "No turns yet. Triggered turns appear here — from a schedule, an email, or a manual run."}
    </p>
  );
}
