import { apiV2 } from "./client.v2";
import { ensureOk, unwrap } from "./problem";
import type { components } from "./generated";

/** An agent's trigger: a schedule (`cron`) or alarm mail from a bound sender
 * (`inbound_mail`). docs/superpowers/specs/2026-10-27-agent-triggers-design.md */
export type Trigger = components["schemas"]["TriggerOut"];
export type TriggerIn = components["schemas"]["TriggerIn"];
export type TriggerPatch = components["schemas"]["TriggerPatch"];
export type TriggerKind = TriggerIn["kind"];

export async function listTriggers(slug: string): Promise<Trigger[]> {
  const res = await apiV2.GET("/api/agents/{slug}/triggers/", {
    params: { path: { slug } },
  });
  const data = unwrap(res, "Failed to load triggers");
  return data.items as Trigger[];
}

/** What a caller must supply; the server's defaults decide the rest (see
 * `createSchedule` in ./schedules for why defaulted fields are not restated). */
type TriggerCreate = Pick<TriggerIn, "name"> & Partial<TriggerIn>;

export async function createTrigger(slug: string, body: TriggerCreate): Promise<Trigger> {
  const res = await apiV2.POST("/api/agents/{slug}/triggers/", {
    params: { path: { slug } },
    body: body as TriggerIn,
  });
  const data = unwrap(res, "Failed to create trigger");
  return data as unknown as Trigger;
}

export async function updateTrigger(slug: string, id: number, body: TriggerPatch): Promise<Trigger> {
  const res = await apiV2.PATCH("/api/agents/{slug}/triggers/{trigger_id}", {
    params: { path: { slug, trigger_id: id } },
    body,
  });
  const data = unwrap(res, "Failed to update trigger");
  return data as unknown as Trigger;
}

export async function deleteTrigger(slug: string, id: number): Promise<void> {
  const res = await apiV2.DELETE("/api/agents/{slug}/triggers/{trigger_id}", {
    params: { path: { slug, trigger_id: id } },
  });
  ensureOk(res, "Failed to delete trigger");
}

/** Fire a cron trigger off-cycle. */
export async function runTriggerNow(slug: string, id: number): Promise<Trigger> {
  const res = await apiV2.POST("/api/agents/{slug}/triggers/{trigger_id}/run-now", {
    params: { path: { slug, trigger_id: id } },
  });
  const data = unwrap(res, "Failed to run trigger");
  return data as unknown as Trigger;
}
