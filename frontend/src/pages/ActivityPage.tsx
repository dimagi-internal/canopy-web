import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { listTurns } from "@/api/turns";
import { EventLedger } from "@/components/activity/EventLedger";
import { EventLogTable, McpCallsTable } from "@/components/activity/WorkspaceLogs";
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
const TABS = [
  { id: "turns", label: "Turns" },
  { id: "events", label: "Event log" },
  { id: "mcp", label: "MCP calls" },
] as const;

export default function ActivityPage() {
  // ?log= picks the log, so a link can open the one you mean.
  const [params, setParams] = useSearchParams();
  const log = TABS.some((t) => t.id === params.get("log")) ? params.get("log")! : "turns";
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
            {log === "turns" ? `Last ${LIMIT} triggered turns · ` : ""}times in {tz}
          </p>
        </div>
        <button type="button" onClick={() => void load()}
          className="rounded border border-border px-2 py-1 text-sm text-foreground hover:bg-muted">
          Refresh
        </button>
      </header>

      <nav className="mb-3 flex gap-1 border-b border-border text-sm" aria-label="Logs">
        {TABS.map((t) => (
          <button key={t.id} type="button"
            onClick={() => setParams((p) => { const n = new URLSearchParams(p); if (t.id === "turns") n.delete("log"); else n.set("log", t.id); return n; })}
            aria-current={log === t.id ? "page" : undefined}
            className={`-mb-px border-b-2 px-3 py-1.5 ${log === t.id ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"}`}>
            {t.label}
          </button>
        ))}
      </nav>

      {log === "events" && <EventLogTable />}
      {log === "mcp" && <McpCallsTable />}
      {log === "turns" && (<>
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
      </>)}
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
            {turn.content_hidden ? (
              <p className="text-xs text-muted-foreground">
                This turn's details are visible to whoever started it, the agent's admins, and workspace admins.
              </p>
            ) : (
              <EventLedger turnId={turn.id} />
            )}
          </td>
        </tr>
      )}
    </>
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
