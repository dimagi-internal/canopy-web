/**
 * Following a long turn: a finished turn's work folds to one line above its
 * reply, a running turn's to-do list is pinned at the top of its work, a
 * person scrolled up is told when something new lands, and the loaded
 * transcript can be searched.
 */
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import { ChatPanel } from "./ChatPanel";
import type { Message, SessionState, TurnStatus } from "./protocol";

afterEach(cleanup);

const ME = 1;
let n = 0;

function msg(over: Partial<Message> = {}): Message {
  n += 1;
  return {
    id: `m${n}`, turn_index: n, role: "assistant", content: {}, plaintext: "",
    status: "complete", error_detail: null, started_at: null, completed_at: null,
    created_at: "2026-10-24T10:00:00Z", ...over,
  };
}
function call(name: string, input: Record<string, unknown>, done = true, at?: string): Message[] {
  const id = `t${(n += 1)}`;
  const use = msg({ role: "tool_use", content: { id, name, input, ...(done ? {} : { status: "pending" }) }, created_at: at ?? "2026-10-24T10:00:00Z" });
  if (!done) return [use];
  return [use, msg({ role: "tool_result", content: { tool_use_id: id }, plaintext: `${name} output`, created_at: at ?? use.created_at })];
}

const TODOS = [
  { content: "Read the code", status: "completed", activeForm: "Reading the code" },
  { content: "Fix the bug", status: "in_progress", activeForm: "Fixing the bug" },
  { content: "Run tests", status: "pending", activeForm: "Running tests" },
];

function finishedTurn(): Message[] {
  return [
    msg({ role: "user", plaintext: "fix the bug", created_at: "2026-10-24T10:00:00Z" }),
    msg({ plaintext: "Looking into it." }),
    ...call("TodoWrite", { todos: TODOS }),
    ...call("Read", { file_path: "apps/x.py" }),
    ...call("Edit", { file_path: "apps/x.py", old_string: "a\nb", new_string: "c" }, true, "2026-10-24T10:04:00Z"),
    msg({ plaintext: "All fixed — the bug was in x.", created_at: "2026-10-24T10:04:12Z" }),
  ];
}

function status(over: Partial<TurnStatus> = {}): TurnStatus {
  return {
    state: "working", agent_slug: "hal", runners: ["jj-mbp"], claimed_by: "jj-mbp", pinned: false,
    cloud_runner: null, cloud_runner_id: null, last_seen_at: null, menu_pending: false,
    settled: false, stuck: false, ...over,
  };
}

function state(over: Partial<SessionState> = {}): SessionState {
  return {
    messages: finishedTurn(),
    active_draft: {
      id: "d1", body: "", version: 1, last_editor: ME, last_edit_at: "2026-10-24T10:00:00Z",
      slot: "next", status: "open",
    },
    participants: [], presence_user_ids: [ME], current_user_id: ME,
    ...over,
  };
}

function panel(s: SessionState) {
  return render(
    <ChatPanel
      state={s} connected currentUserId={ME}
      onSend={vi.fn()} onStop={vi.fn()} onUpdateDraft={vi.fn()} onDiscard={vi.fn()}
    />,
  );
}

describe("a finished turn", () => {
  it("folds its work into one line, with the final reply in full below", () => {
    panel(state({ turn_status: status({ state: "done", settled: true }) }));
    const fold = screen.getByTestId("turn-fold");
    expect(screen.getByTestId("turn-fold-line").textContent).toBe(
      "Worked for 4m 12s · 3 tool calls · 1 file changed",
    );
    expect((fold as HTMLDetailsElement).open).toBe(false);
    // The work is inside the fold; the reply is outside it.
    expect(within(fold).getByText("Looking into it.")).not.toBeNull();
    const reply = screen.getByText("All fixed — the bug was in x.");
    expect(fold.contains(reply)).toBe(false);
    // Opened, it lists the files with their line counts.
    const files = within(fold).getByTestId("files-changed");
    expect(files.textContent).toContain("+1");
    expect(files.textContent).toContain("−2");
    expect(files.textContent).toContain("apps/x.py");
  });

  it("shows its TodoWrite row as a checklist, not JSON", () => {
    panel(state({ turn_status: status({ state: "done", settled: true }) }));
    const row = screen.getByTestId("todowrite-row");
    expect(row.textContent).toContain("1/3 done");
    expect(within(row).getByText("Fixing the bug")).not.toBeNull();
    expect(row.textContent).not.toContain('"status"');
  });
});

describe("a running turn", () => {
  it("stays expanded, with its latest to-do list pinned at the top of its work", () => {
    const messages = [...finishedTurn().slice(0, -1), ...call("Bash", { command: "npm test" }, false)];
    panel(state({ messages, turn_status: status() }));
    expect(screen.queryByTestId("turn-fold")).toBeNull();
    const turn = screen.getAllByTestId("turn")[0];
    const pinned = within(turn).getAllByTestId("todo-checklist")[0];
    expect(pinned.className).toContain("sticky");
    // ◐ in progress is said in its "doing" words.
    const items = pinned.querySelectorAll("li");
    expect([...items].map((li) => li.dataset.status)).toEqual(["completed", "in_progress", "pending"]);
    expect(items[1].textContent).toContain("Fixing the bug");
    expect(screen.getByTestId("activity-text").textContent).toBe("Running npm test");
  });

  it("is not folded while the agent is blocked on a question", () => {
    panel(state({ activity: "blocked", turn_status: status({ state: "blocked", menu_pending: true, stuck: true }) }));
    expect(screen.queryByTestId("turn-fold")).toBeNull();
  });
});

describe("jump to latest", () => {
  function scrolledUp(el: HTMLElement) {
    Object.defineProperty(el, "scrollHeight", { configurable: true, value: 2000 });
    Object.defineProperty(el, "clientHeight", { configurable: true, value: 500 });
    el.scrollTop = 200;
    fireEvent.scroll(el);
  }

  it("appears when scrolled up, says so when new rows land, and goes back down", () => {
    const s = state({ turn_status: status({ state: "done", settled: true }) });
    const view = panel(s);
    expect(screen.queryByTestId("jump-to-latest")).toBeNull();
    const scroller = screen.getByTestId("chat-scroll");
    scrolledUp(scroller);
    expect(screen.getByTestId("jump-to-latest").textContent).toBe("Jump to latest");
    view.rerender(
      <ChatPanel
        state={{ ...s, messages: [...s.messages, msg({ role: "user", plaintext: "and another" })] }}
        connected currentUserId={ME}
        onSend={vi.fn()} onStop={vi.fn()} onUpdateDraft={vi.fn()} onDiscard={vi.fn()}
      />,
    );
    expect(screen.getByTestId("jump-to-latest").textContent).toBe("New messages");
    fireEvent.click(screen.getByTestId("jump-to-latest"));
    expect(scroller.scrollTop).toBe(2000);
    expect(screen.queryByTestId("jump-to-latest")).toBeNull();
  });

  // Reading just above the bottom while a reply streams in: the first pixels
  // of a scroll-up must already stop the view following, or each new chunk
  // snaps the reader back down before they get anywhere.
  function tall(el: HTMLElement) {
    Object.defineProperty(el, "scrollHeight", { configurable: true, value: 2000 });
    Object.defineProperty(el, "clientHeight", { configurable: true, value: 500 });
  }
  function grow(view: ReturnType<typeof panel>, s: ReturnType<typeof state>, text: string) {
    view.rerender(
      <ChatPanel
        state={{ ...s, messages: [...s.messages, msg({ plaintext: text })] }}
        connected currentUserId={ME}
        onSend={vi.fn()} onStop={vi.fn()} onUpdateDraft={vi.fn()} onDiscard={vi.fn()}
      />,
    );
  }

  it("stops following on a small scroll up, even close to the bottom", () => {
    const s = state({ turn_status: status({ state: "done", settled: true }) });
    const view = panel(s);
    const scroller = screen.getByTestId("chat-scroll");
    tall(scroller);
    scroller.scrollTop = 1500;
    fireEvent.scroll(scroller);
    scroller.scrollTop = 1460;
    fireEvent.scroll(scroller);
    grow(view, s, "more text");
    expect(scroller.scrollTop).toBe(1460);
    expect(screen.getByTestId("jump-to-latest").textContent).toBe("New messages");
  });

  it("stops following when the wheel turns up, before the scroll lands", () => {
    const s = state({ turn_status: status({ state: "done", settled: true }) });
    const view = panel(s);
    const scroller = screen.getByTestId("chat-scroll");
    tall(scroller);
    scroller.scrollTop = 1500;
    fireEvent.scroll(scroller);
    fireEvent.wheel(scroller, { deltaY: -40 });
    grow(view, s, "more text");
    expect(scroller.scrollTop).toBe(1500);
  });

  it("follows again once scrolled back down near the bottom", () => {
    const s = state({ turn_status: status({ state: "done", settled: true }) });
    const view = panel(s);
    const scroller = screen.getByTestId("chat-scroll");
    tall(scroller);
    scroller.scrollTop = 800;
    fireEvent.scroll(scroller);
    scroller.scrollTop = 1450;
    fireEvent.scroll(scroller);
    grow(view, s, "more text");
    expect(scroller.scrollTop).toBe(2000);
    expect(screen.queryByTestId("jump-to-latest")).toBeNull();
  });
});

describe("find in transcript", () => {
  it("opens on Cmd/Ctrl+F, counts matches in loaded messages, and opens a folded match", () => {
    panel(state({ turn_status: status({ state: "done", settled: true }) }));
    fireEvent.keyDown(screen.getByTestId("chat-scroll"), { key: "f", metaKey: true });
    const box = screen.getByRole("textbox", { name: "Find in loaded messages" });
    expect(screen.getByTestId("chat-find-count").textContent).toBe("loaded messages only");
    act(() => {
      fireEvent.change(box, { target: { value: "looking into" } });
    });
    expect(screen.getByTestId("chat-find-count").textContent).toBe("1 of 1");
    // The match was folded shut; finding it opened the fold.
    expect((screen.getByTestId("turn-fold") as HTMLDetailsElement).open).toBe(true);
    act(() => {
      fireEvent.change(box, { target: { value: "nowhere-in-here" } });
    });
    expect(screen.getByTestId("chat-find-count").textContent).toBe("No matches");
    fireEvent.keyDown(box, { key: "Escape" });
    expect(screen.queryByTestId("chat-find")).toBeNull();
  });

  it("steps through several matches", () => {
    panel(state({ turn_status: status({ state: "done", settled: true }) }));
    fireEvent.click(screen.getByRole("button", { name: "Find in loaded messages" }));
    const box = screen.getByRole("textbox", { name: "Find in loaded messages" });
    act(() => {
      fireEvent.change(box, { target: { value: "x.py" } });
    });
    const count = screen.getByTestId("chat-find-count");
    const total = Number(count.textContent?.split(" of ")[1]);
    expect(total).toBeGreaterThan(1);
    fireEvent.keyDown(box, { key: "Enter" });
    expect(count.textContent).toBe(`2 of ${total}`);
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(count.textContent).toBe(`${total} of ${total}`);
  });
});
