import { describe, expect, it } from "vitest";
import {
  backfillAction,
  restToKitMessage,
  sendBlockReason,
  shouldShowLoadFull,
} from "./chatPageLogic";
import type { ChatSessionDetail } from "@/api/chat";

describe("restToKitMessage", () => {
  it("maps a REST MessageOut into the kit Message shape as complete", () => {
    const rest: ChatSessionDetail["messages"][number] = {
      turn_index: 7,
      role: "assistant",
      plaintext: "hello",
      content: { text: "hello" },
      created_at: "2026-07-23T00:00:00Z",
    };
    expect(restToKitMessage(rest)).toEqual({
      id: "t7",
      turn_index: 7,
      role: "assistant",
      content: { text: "hello" },
      plaintext: "hello",
      status: "complete",
      error_detail: null,
      started_at: null,
      completed_at: "2026-07-23T00:00:00Z",
      created_at: "2026-07-23T00:00:00Z",
    });
  });

  it("gives every mapped row a synthetic id distinct from a real WS pk", () => {
    const rest: ChatSessionDetail["messages"][number] = {
      turn_index: 3,
      role: "user",
      plaintext: "hi",
      content: {},
      created_at: "2026-07-23T00:00:00Z",
    };
    // The kit dedupes prepended history by turn_index, not id — but the id
    // must still never collide with a real WS message pk.
    expect(restToKitMessage(rest).id).toBe("t3");
  });
});

describe("backfillAction", () => {
  it("maps ready -> reload-now", () => {
    expect(backfillAction("ready")).toBe("reload-now");
  });

  it("maps requested -> reload-after-delay", () => {
    expect(backfillAction("requested")).toBe("reload-after-delay");
  });

  it("maps unavailable -> unavailable", () => {
    expect(backfillAction("unavailable")).toBe("unavailable");
  });

  it("degrades an unrecognized status to an immediate reload", () => {
    expect(backfillAction("something-new")).toBe("reload-now");
  });
});

describe("shouldShowLoadFull", () => {
  const base = { hasMoreBefore: false, historyUnavailable: false };

  // REGRESSION (found on prod): a runner-discovered session whose history is
  // still on the laptop has ZERO server Message rows, so it rendered "Start the
  // conversation" with no way to pull its transcript. The offer must NOT depend
  // on messages already being on screen.
  it("offers Load full for a runner session with no messages yet", () => {
    expect(shouldShowLoadFull({ ...base, origin: "runner" })).toBe(true);
  });

  it("does not offer it for a web session", () => {
    expect(shouldShowLoadFull({ ...base, origin: "web" })).toBe(false);
  });

  it("defers to Load earlier when the server holds more than the window", () => {
    expect(
      shouldShowLoadFull({ ...base, origin: "runner", hasMoreBefore: true }),
    ).toBe(false);
  });

  it("stays hidden once history is known unavailable", () => {
    expect(
      shouldShowLoadFull({
        ...base,
        origin: "runner",
        historyUnavailable: true,
      }),
    ).toBe(false);
  });

  it("is hidden before the session meta loads (origin null/undefined)", () => {
    expect(shouldShowLoadFull({ ...base, origin: null })).toBe(false);
    expect(shouldShowLoadFull({ ...base, origin: undefined })).toBe(false);
  });
});

describe("sendBlockReason", () => {
  it("allows sending when the bound runner is fine", () => {
    expect(
      sendBlockReason({ runnerName: "Laptop", boundOffline: false, paused: false }),
    ).toBeUndefined();
  });

  it("blocks with the resume instruction when the runner is paused", () => {
    // The fix is one tap and it undoes YOUR decision — say so, rather than
    // offering the generic "unavailable" that implies something broke.
    expect(
      sendBlockReason({ runnerName: "Laptop", boundOffline: true, paused: true }),
    ).toBe("Laptop is paused — resume it to send");
  });

  it("blocks with the re-place instruction when the runner is merely gone", () => {
    expect(
      sendBlockReason({ runnerName: "Laptop", boundOffline: true, paused: false }),
    ).toBe("Laptop is unavailable — continue on another runner to send");
  });

  it("fails OPEN when there is no bound runner at all", () => {
    // A web chat that has never sent has no binding; blocking it would lock the
    // composer of a brand-new conversation. Over-blocking is worse than the
    // queue this prevents.
    for (const runnerName of [null, undefined, ""]) {
      expect(sendBlockReason({ runnerName, boundOffline: true, paused: false })).toBeUndefined();
    }
  });
});
