import { apiV2 } from "./client.v2";
import { ensureOk, unwrap } from "./problem";
import type { components } from "./generated";

export type Schedule = components["schemas"]["ScheduleOut"];
export type ScheduleIn = components["schemas"]["ScheduleIn"];
export type SchedulePatch = components["schemas"]["SchedulePatch"];

export async function listSchedules(slug: string): Promise<Schedule[]> {
  const res = await apiV2.GET("/api/agents/{slug}/schedules/", {
    params: { path: { slug } },
  });
  const data = unwrap(res, "Failed to load schedules");
  return data.items as Schedule[];
}

/** What a caller must actually supply to create a schedule. `openapi-typescript`
 * emits the server's DEFAULTED fields (enabled / routing / grace_minutes /
 * notify) as required, which would force every call site to restate defaults
 * that already live in the Ninja schema — and silently drift from them the day
 * one changes. Narrowing here keeps the server the single source of truth:
 * omit a field and the server decides. */
type ScheduleCreate = Pick<ScheduleIn, "name" | "prompt" | "cron"> &
  Partial<ScheduleIn>;

export async function createSchedule(
  slug: string,
  body: ScheduleCreate,
): Promise<Schedule> {
  const res = await apiV2.POST("/api/agents/{slug}/schedules/", {
    params: { path: { slug } },
    body: body as ScheduleIn,
  });
  const data = unwrap(res, "Failed to create schedule");
  return data as unknown as Schedule;
}

export async function updateSchedule(
  slug: string,
  id: number,
  body: SchedulePatch,
): Promise<Schedule> {
  const res = await apiV2.PATCH(
    "/api/agents/{slug}/schedules/{schedule_id}",
    {
      params: { path: { slug, schedule_id: id } },
      body,
    },
  );
  const data = unwrap(res, "Failed to update schedule");
  return data as unknown as Schedule;
}

export async function deleteSchedule(slug: string, id: number): Promise<void> {
  const res = await apiV2.DELETE(
    "/api/agents/{slug}/schedules/{schedule_id}",
    {
      params: { path: { slug, schedule_id: id } },
    },
  );
  ensureOk(res, "Failed to delete schedule");
}

export async function runScheduleNow(
  slug: string,
  id: number,
): Promise<Schedule> {
  const res = await apiV2.POST(
    "/api/agents/{slug}/schedules/{schedule_id}/run-now",
    {
      params: { path: { slug, schedule_id: id } },
    },
  );
  const data = unwrap(res, "Failed to run schedule");
  return data as unknown as Schedule;
}

export async function previewCron(
  slug: string,
  cron: string,
  timezone: string,
): Promise<string[]> {
  const res = await apiV2.POST(
    "/api/agents/{slug}/schedules/preview",
    {
      params: { path: { slug } },
      body: { cron, timezone },
    },
  );
  const data = unwrap(res, "Failed to preview schedule");
  return data.next_runs as string[];
}

export type ScheduleWeekItem = components["schemas"]["ScheduledFireOut"];

/** A week of scheduled fires across every schedule the caller can see. Scope is
 * the CURRENT ROUTE: on /schedules (root) this hits the flat path and spans all
 * your workspaces; under /w/:ws/… the client middleware rewrites it to the
 * tenant path and the server pins that workspace. No ws param needed. */
export async function getScheduleWeek(start: string): Promise<ScheduleWeekItem[]> {
  const res = await apiV2.GET("/api/agents/schedules/week", {
    params: { query: { start } },
  });
  const data = unwrap(res, "Failed to load the schedule");
  return data.items as ScheduleWeekItem[];
}
