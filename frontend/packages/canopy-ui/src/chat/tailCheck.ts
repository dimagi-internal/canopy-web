import type { Message } from "./protocol";
import { isUnconfirmed } from "./identity";

/**
 * Does the page's conversation match the server's?
 *
 * The open page builds its list from several sources — its own optimistic
 * lines, receipts, live frames, reconnect snapshots, older pages — and a reload
 * reads one: the server's rows. Each merge has its rules, and a rule that is
 * wrong shows up only as a page that differs from a reload. This compares the
 * two over the server's tail, so a host can put the page right and say that it
 * had to.
 *
 * Only durable rows count: a line of yours still waiting for its echo has no
 * server row yet, and a row older than the tail is not in what was read.
 */
export interface TailDivergence {
  /** Server rows the page does not hold. */
  missing: string[];
  /** Rows the page holds, within the tail's range, that the server does not. */
  extra: string[];
  /** Both hold the same rows, in a different order. */
  reordered: boolean;
}

export function tailDivergence(local: readonly Message[], server: readonly Message[]): TailDivergence | null {
  if (server.length === 0) return null;
  const lowest = server.reduce((acc, m) => Math.min(acc, m.turn_index), Infinity);
  const serverIds = new Set(server.map((m) => m.id));
  const held = local.filter((m) => !isUnconfirmed(m) && m.turn_index >= lowest);
  const heldIds = new Set(held.map((m) => m.id));
  const missing = server.filter((m) => !heldIds.has(m.id)).map((m) => m.id);
  const extra = held.filter((m) => !serverIds.has(m.id)).map((m) => m.id);
  const order = (rows: readonly Message[]) => rows.filter((m) => serverIds.has(m.id) && heldIds.has(m.id)).map((m) => m.id);
  const a = order(held);
  const b = order(server);
  const reordered = a.some((id, i) => id !== b[i]);
  return missing.length || extra.length || reordered ? { missing, extra, reordered } : null;
}
