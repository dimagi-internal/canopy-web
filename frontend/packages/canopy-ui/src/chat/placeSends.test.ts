import { describe, expect, it } from "vitest";

import type { Message, QueuedMessage, TurnStatus } from "./protocol";
import { placeSends } from "./placeSends";

const me = { userId: 1, contactId: null };
const row = (over: Partial<Message>): Message => ({
  id: "x", turn_index: 1, role: "user", content: {}, plaintext: "", status: "complete",
  error_detail: null, started_at: null, completed_at: null, created_at: "", ...over,
});
const queued = (over: Partial<QueuedMessage>): QueuedMessage =>
  ({ turn_id: "t1", client_id: "c1", author: { name: "Me", user_id: 1 }, text: "hi", sent_at: "", state: "queued", ...over }) as QueuedMessage;
const working = { state: "working", settled: false } as TurnStatus;

describe("placeSends", () => {
  it("the head of the queue on an idle box keeps its bubble (no flicker)", () => {
    const mine = row({ id: "local:c1", status: "pending", content: { client_id: "c1" }, plaintext: "hi" });
    const p = placeSends([mine], [queued({})], me, null);
    expect(p.shown).toEqual([mine]);
    expect(p.sendInQueue).toBe(false);
    expect(p.hideClientIds.has("c1")).toBe(true);
    expect(p.cancellable.get("c1")).toBe("t1");
    expect(p.ownPendingTexts.has("hi")).toBe(true);
  });

  it("a send held behind a running turn is drawn in the queue, not the transcript", () => {
    const mine = row({ id: "local:c1", status: "pending", content: { client_id: "c1" } });
    const p = placeSends([row({ id: "a", role: "assistant" }), mine], [queued({})], me, working);
    expect(p.shown.map((m) => m.id)).toEqual(["a"]);
    expect(p.sendInQueue).toBe(true);
    expect(p.hideClientIds.has("c1")).toBe(false);
  });

  it("someone else's queued send is never cancellable by me", () => {
    const p = placeSends([], [queued({ author: { name: "Ana", user_id: 2 } })], me, working);
    expect(p.cancellable.size).toBe(0);
  });
});
