/**
 * Pressing send must change the TRANSCRIPT, not just the header.
 *
 * The gap this closes: no assistant row exists until `chat.stream_start`, and
 * the server emits that together with `stream_complete` from one `assistant`
 * ledger event (apps/canopy_sessions/stream_map.py) — i.e. only once the reply's
 * first text exists, which on a laptop runner is a claim poll plus an emdash
 * drive plus however long the agent thinks. So the conversation sat visibly
 * unchanged for seconds-to-minutes after a send, which reads as the app having
 * ignored you. Reported on a phone, twice, after the header-chip fix (#490)
 * had supposedly addressed it.
 */
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { ChatPanel } from "./ChatPanel";
import type { Message, SessionState, TurnStatus } from "./protocol";

afterEach(cleanup);

const ME = 1;

function message(overrides: Partial<Message> = {}): Message {
  return {
    id: "m1",
    turn_index: 1,
    role: "user",
    content: { text: "hi" },
    plaintext: "hi",
    status: "complete",
    error_detail: null,
    started_at: null,
    completed_at: null,
    created_at: "2026-07-30T00:00:00Z",
    ...overrides,
  };
}

function state(overrides: Partial<SessionState> = {}): SessionState {
  return {
    messages: [message()],
    active_draft: {
      id: "d1",
      body: "",
      version: 1,
      last_editor: ME,
      last_edit_at: "2026-07-30T00:00:00Z",
      slot: "next",
      status: "open",
    },
    participants: [],
    presence_user_ids: [ME],
    current_user_id: ME,
    ...overrides,
  };
}

function panel(props: Partial<Parameters<typeof ChatPanel>[0]> = {}) {
  return render(
    <ChatPanel
      state={state()}
      connected
      currentUserId={ME}
      onSend={vi.fn()}
      onStop={vi.fn()}
      onUpdateDraft={vi.fn()}
      onTakeOver={vi.fn()}
      onDiscard={vi.fn()}
      {...props}
    />,
  );
}

describe("the pending-reply row", () => {
  it("is absent while nothing is outstanding", () => {
    panel();
    expect(screen.queryByTestId("pending-reply")).toBeNull();
  });

  it("appears the instant a send goes out, with no server round trip", () => {
    // `awaitingReply` is set synchronously by sendChat. NOTHING server-side can
    // answer sooner: the turn has to be enqueued, claimed, and driven into the
    // agent before any report could exist.
    panel({ awaitingReply: true });
    expect(screen.getByTestId("pending-reply")).not.toBeNull();
    expect(screen.getByText("Queued…")).not.toBeNull();
  });

  it("says the agent is thinking once the hook reports it started", () => {
    // The distinction is the useful part: queued means canopy has not started
    // your turn, thinking means the agent has it. Folding them together hides
    // where the delay actually is.
    panel({ state: state({ activity: "working" }), awaitingReply: false });
    expect(screen.getByText("Thinking…")).not.toBeNull();
  });

  it("survives the first reply block, because the turn has not ended", () => {
    // `awaitingReply` clears on the FIRST stream_complete, but the bridge posts
    // each assistant block as its own event — so a turn that says one sentence
    // and then works for another minute would otherwise go silent again.
    panel({ state: state({ activity: "working" }), awaitingReply: false });
    expect(screen.getByTestId("pending-reply")).not.toBeNull();
  });

  it("withdraws when the agent is BLOCKED on you", () => {
    // The worst way to get this wrong: waiting on an agent that is waiting on
    // you. `blocked` must never render as work in progress.
    panel({ state: state({ activity: "blocked" }), awaitingReply: true });
    expect(screen.queryByTestId("pending-reply")).toBeNull();
  });

  it("yields to a real assistant row once one exists", () => {
    // Otherwise the reply and the placeholder are both on screen at once.
    panel({
      state: state({
        messages: [
          message(),
          message({ id: "m2", turn_index: 2, role: "assistant", plaintext: "", content: {}, status: "streaming" }),
        ],
      }),
      awaitingReply: true,
    });
    expect(screen.queryByTestId("pending-reply")).toBeNull();
  });

  it("shows on a first-ever send, when the empty state would otherwise win", () => {
    panel({
      state: state({ messages: [] }),
      awaitingReply: true,
      emptyState: <div>no messages yet</div>,
    });
    expect(screen.getByTestId("pending-reply")).not.toBeNull();
    expect(screen.queryByText("no messages yet")).toBeNull();
  });
});

function status(over: Partial<TurnStatus> = {}): TurnStatus {
  return {
    state: "working",
    agent_slug: "hal",
    runners: ["jj-mbp"],
    claimed_by: "jj-mbp",
    pinned: false,
    cloud_runner: null,
    cloud_runner_id: null,
    last_seen_at: null,
    menu_pending: false,
    settled: false,
    stuck: false,
    ...over,
  };
}

describe("the server's turn status", () => {
  it("replaces the spinner with a reason when nothing will pick the turn up", () => {
    // The regression the whole projection exists for. A client cannot know
    // this: it pressed send, and from here an offline runner and a slow one
    // are identical.
    panel({
      state: state({
        turn_status: status({ state: "waiting_runner", stuck: true, claimed_by: null }),
      }),
      awaitingReply: true,
    });
    expect(screen.queryByTestId("pending-reply")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("jj-mbp is offline");
  });

  it("reads differently when waiting cannot possibly help", () => {
    panel({
      state: state({
        turn_status: status({ state: "unrouted", stuck: true, runners: [], claimed_by: null }),
      }),
      awaitingReply: true,
    });
    expect(screen.getByRole("status").textContent).toContain("no runner is set up to run hal");
  });

  it("names the box in the spinner once a runner really has it", () => {
    panel({ state: state({ turn_status: status({ state: "working" }) }) });
    expect(screen.getByText("Thinking on jj-mbp…")).not.toBeNull();
  });

  it("clears a stuck spinner even if the stream frame that ends it never arrived", () => {
    // `awaitingReply` only clears on a stream frame; a dropped socket used to
    // leave it spinning forever. The server's settled flag is the backstop.
    panel({
      state: state({ turn_status: status({ state: "done", settled: true }) }),
      awaitingReply: true,
    });
    expect(screen.queryByTestId("pending-reply")).toBeNull();
  });

  it("stays silent when the host has its own, richer banner for the same fact", () => {
    // canopy's chat page raises <PlacementBanner> here, with buttons to wait
    // or continue elsewhere. Our sentence underneath would restate it as a
    // dead end directly below the thing you can actually press.
    panel({
      state: state({
        turn_status: status({ state: "waiting_runner", stuck: true, claimed_by: null }),
      }),
      banner: <div>continue on another runner?</div>,
    });
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("continue on another runner?")).not.toBeNull();
  });

  it("leaves a blocked agent entirely to the menu", () => {
    panel({ state: state({ turn_status: status({ state: "blocked", stuck: true }) }) });
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByTestId("pending-reply")).toBeNull();
  });
});
