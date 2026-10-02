import { useEffect, useState } from "react";
import { listEvents, listMcpCalls, type LogEvent, type McpCall } from "@/api/logs";
import { relativeTime } from "@/components/activity/turnLog";

// The two logs beside the turn log on Activity. Both are the workspace
// ADMIN's (apps/workspaces/permissions.py, LOGS_READ): an admin or owner sees
// everything in their workspace; anyone else sees an empty event log and only
// their own MCP calls — the server decides, this only says so.

function useLoad<T>(load: () => Promise<T[]>, deps: unknown[]) {
  const [rows, setRows] = useState<T[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    setRows(null);
    load()
      .then((r) => { if (alive) { setRows(r); setError(""); } })
      .catch((e) => { if (alive) { setError(e instanceof Error ? e.message : "Failed to load"); setRows([]); } });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { rows, error };
}

const LEVEL_TOKEN: Record<string, string> = {
  error: "bg-destructive/10 text-destructive border-destructive/30",
  warn: "bg-warning/10 text-warning border-warning/30",
  info: "bg-info/10 text-info border-info/30",
};

export function EventLogTable() {
  const { rows, error } = useLoad<LogEvent>(() => listEvents(100), []);
  const now = new Date();
  if (error) return <p className="text-sm text-destructive">{error}</p>;
  if (rows === null) return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (rows.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="event-log-empty">
        Nothing here. The event log is read by workspace admins and owners.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto rounded border border-border">
      <table className="w-full text-sm" data-testid="event-log">
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted-foreground">
            <th className="px-3 py-2 font-medium">Last seen</th>
            <th className="px-3 py-2 font-medium">Level</th>
            <th className="px-3 py-2 font-medium">Source</th>
            <th className="px-3 py-2 font-medium">What</th>
            <th className="px-3 py-2 font-medium">Count</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((e) => (
            <tr key={e.id} className="border-b border-border last:border-0">
              <td className="px-3 py-2 text-foreground-secondary" title={new Date(e.last_seen_at).toLocaleString()}>
                {relativeTime(e.last_seen_at, now)}
              </td>
              <td className="px-3 py-2">
                <span className={`inline-block rounded border px-1.5 py-0.5 text-xs ${LEVEL_TOKEN[e.level] ?? "border-border text-muted-foreground"}`}>
                  {e.level}
                </span>
              </td>
              <td className="px-3 py-2 text-muted-foreground">{e.source} · {e.kind}</td>
              <td className="px-3 py-2 text-foreground">{e.summary || e.key}</td>
              <td className="px-3 py-2 tabular-nums text-foreground-secondary">{e.count}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function McpCallsTable() {
  const [failedOnly, setFailedOnly] = useState(false);
  const { rows, error } = useLoad<McpCall>(() => listMcpCalls(100, failedOnly), [failedOnly]);
  const now = new Date();
  return (
    <div className="space-y-2">
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        <input type="checkbox" checked={failedOnly} onChange={(e) => setFailedOnly(e.target.checked)} />
        Failed calls only
      </label>
      {error ? (
        <p className="text-sm text-destructive">{error}</p>
      ) : rows === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="mcp-calls-empty">
          No calls. Admins and owners see every MCP call made in their workspace; everyone sees their own.
        </p>
      ) : (
        <div className="overflow-x-auto rounded border border-border">
          <table className="w-full text-sm" data-testid="mcp-calls">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium">Who</th>
                <th className="px-3 py-2 font-medium">Tool</th>
                <th className="px-3 py-2 font-medium">Call</th>
                <th className="px-3 py-2 font-medium">Result</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.id} className="border-b border-border last:border-0">
                  <td className="px-3 py-2 text-foreground-secondary" title={new Date(c.created_at).toLocaleString()}>
                    {relativeTime(c.created_at, now)}
                  </td>
                  <td className="px-3 py-2 text-foreground">{c.user_email || "—"}</td>
                  <td className="px-3 py-2 font-mono text-xs text-foreground">{c.tool}</td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{c.args_summary}</td>
                  <td className="px-3 py-2 text-xs">
                    {c.ok ? <span className="text-success">ok</span>
                      : <span className="text-destructive" title={c.error}>failed</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
