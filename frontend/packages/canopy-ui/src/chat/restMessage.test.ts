import { describe, expect, it } from "vitest";

import { prependHistory } from "./history";
import { restToKitMessage, type RestMessage } from "./restMessage";
import type { Message } from "./protocol";

const row: RestMessage = {
  turn_index: 7,
  role: "assistant",
  content: { text: "hello" },
  plaintext: "hello",
  created_at: "2026-09-16T10:00:00Z",
};

describe("restToKitMessage", () => {
  it("maps a REST row onto the kit's Message shape", () => {
    expect(restToKitMessage(row)).toEqual({
      id: "t7",
      turn_index: 7,
      role: "assistant",
      content: { text: "hello" },
      plaintext: "hello",
      status: "complete",
      error_detail: null,
      started_at: null,
      completed_at: "2026-09-16T10:00:00Z",
      created_at: "2026-09-16T10:00:00Z",
    });
  });

  it("gives a row read back from REST no streaming history", () => {
    // A REST row was never watched arriving, so claiming a `started_at` would
    // invent a fact. `completed_at` is what the server does know.
    const m = restToKitMessage(row);
    expect(m.started_at).toBeNull();
    expect(m.completed_at).toBe(row.created_at);
  });

  it("accepts a readonly generated row (what both hosts actually pass)", () => {
    // openapi-typescript emits every field `readonly`. Property readonly-ness
    // does not affect assignability, and this asserts that stays true — it is
    // the reason `RestMessage` can be declared structurally instead of as
    // either host's generated type.
    const generated: { readonly [K in keyof RestMessage]: RestMessage[K] } = row;
    expect(restToKitMessage(generated).id).toBe("t7");
  });

  it("does not collide with the live WS row for the same turn", () => {
    // The synthetic `t<turn_index>` id is only safe because prependHistory
    // dedupes on turn_index. If that ever changed to dedupe on id, this fails.
    const live: Message = { ...restToKitMessage(row), id: "1234", status: "streaming" };
    const merged = prependHistory([live], [restToKitMessage(row)]);
    expect(merged).toHaveLength(1);
    expect(merged[0].id).toBe("1234");
  });
});
