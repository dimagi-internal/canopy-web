import { apiV2 } from "./client.v2";
import type { components } from "./generated";
import { unwrap } from "./problem";

export type Shareout = components["schemas"]["ShareoutOut"];
export type ShareoutLink = components["schemas"]["ShareoutLink"];
export type ShareoutPR = components["schemas"]["ShareoutPR"];

export interface ShareoutListParams {
  date_from?: string; // YYYY-MM-DD
  date_to?: string;
  project?: string;
  limit?: number;
}

// A period groups every shareout sharing the same start..end window. The
// roll-up (project_slug === null) renders first within a period; the rest are
// per-project cards.
export interface ShareoutPeriod {
  key: string;
  periodStart: string;
  periodEnd: string;
  rollup: Shareout | null;
  projects: Shareout[];
}

function periodKey(s: Shareout): string {
  return `${s.period_start}..${s.period_end}`;
}

export function groupByPeriod(shareouts: Shareout[]): ShareoutPeriod[] {
  const byKey = new Map<string, ShareoutPeriod>();
  for (const s of shareouts) {
    const key = periodKey(s);
    let period = byKey.get(key);
    if (!period) {
      period = {
        key,
        periodStart: s.period_start,
        periodEnd: s.period_end,
        rollup: null,
        projects: [],
      };
      byKey.set(key, period);
    }
    if (s.project_slug == null) period.rollup = s;
    else period.projects.push(s);
  }
  // Newest period first (list arrives newest-first, but be explicit).
  return [...byKey.values()].sort((a, b) =>
    b.periodEnd.localeCompare(a.periodEnd),
  );
}

export const shareoutsApi = {
  list: async (params: ShareoutListParams = {}): Promise<Shareout[]> => {
    const res = await apiV2.GET("/api/shareouts/", {
      params: {
        query: {
          ...(params.date_from ? { date_from: params.date_from } : {}),
          ...(params.date_to ? { date_to: params.date_to } : {}),
          ...(params.project ? { project: params.project } : {}),
          ...(params.limit !== undefined ? { limit: params.limit } : {}),
        },
      },
    });
    const data = unwrap(res, "Failed to load shareouts");
    return data.items as Shareout[];
  },
};
