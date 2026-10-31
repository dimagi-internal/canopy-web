import { describe, expect, it } from "vitest";

import { pairToolMessages } from "./pairToolMessages";
import type { Message } from "./protocol";
import {
  countLines,
  currentActivity,
  describeCall,
  filesChanged,
  foldLine,
  formatElapsed,
  latestTodos,
  planFold,
  segmentTurns,
} from "./turnWork";

let n = 0;
function msg(over: Partial<Message> = {}): Message {
  n += 1;
  return {
    id: `m${n}`, turn_index: n, role: "assistant", content: {}, plaintext: "",
    status: "complete", error_detail: null, started_at: null, completed_at: null,
    created_at: "2026-10-24T10:00:00Z", ...over,
  };
}
const user = (text: string, at = "2026-10-24T10:00:00Z") =>
  msg({ role: "user", plaintext: text, created_at: at });
const say = (text: string, at = "2026-10-24T10:00:00Z") =>
  msg({ role: "assistant", plaintext: text, created_at: at });
function call(name: string, input: Record<string, unknown>, result: Partial<Message> | null = {}, at?: string): Message[] {
  const id = `t${(n += 1)}`;
  const use = msg({ role: "tool_use", content: { id, name, input }, created_at: at ?? "2026-10-24T10:00:00Z" });
  if (result === null) return [use];
  return [use, msg({ role: "tool_result", content: { tool_use_id: id }, plaintext: "ok", created_at: at ?? use.created_at, ...result })];
}

describe("segmentTurns", () => {
  it("starts a turn at each person's line, keeping rows before the first apart", () => {
    const rows = pairToolMessages([
      msg({ role: "system", plaintext: "seed" }),
      user("one"), say("a"),
      user("two"), ...call("Bash", { command: "ls" }), say("b"),
    ]);
    const turns = segmentTurns(rows);
    expect(turns.map((t) => (t.user ? "turn" : "pre"))).toEqual(["pre", "turn", "turn"]);
    expect(turns[2].body).toHaveLength(2);
  });

  it("does not start a turn at a compaction summary", () => {
    const rows = pairToolMessages([
      user("go"), say("x"),
      msg({ role: "user", plaintext: "summary", content: { record: { compact_summary: true } } }),
      say("y"),
    ]);
    expect(segmentTurns(rows)).toHaveLength(1);
  });
});

describe("planFold", () => {
  const turn = () =>
    segmentTurns(
      pairToolMessages([
        user("fix it", "2026-10-24T10:00:00Z"),
        say("Looking."),
        ...call("Read", { file_path: "a.py" }),
        ...call("Edit", { file_path: "a.py", old_string: "x\ny", new_string: "z" }, {}, "2026-10-24T10:03:00Z"),
        say("Fixed it.", "2026-10-24T10:04:12Z"),
      ]),
    )[0];

  it("folds a finished turn's tool calls above its final reply", () => {
    const fold = planFold(turn(), { live: false });
    expect(fold).not.toBeNull();
    expect(fold!.folded.map((r) => r.kind)).toEqual(["tool_pair", "tool_pair"]);
    expect(fold!.reply.kind === "message" && fold!.reply.message.plaintext).toBe("Fixed it.");
    expect(foldLine(fold!.summary)).toBe("Worked for 4m 12s · 2 tool calls · 1 file changed");
  });

  it("never folds what the agent said along the way", () => {
    // It was on screen while the turn ran; folding it at the end pulled text
    // out from under someone reading it.
    const fold = planFold(turn(), { live: false });
    expect(fold!.kept.map((r) => r.kind === "message" && r.message.plaintext)).toEqual(["Looking."]);
  });

  it("never folds a live turn", () => {
    expect(planFold(turn(), { live: true })).toBeNull();
  });

  it("takes `live` as given: a call whose result never came does not hold a finished turn open", () => {
    const t = segmentTurns(pairToolMessages([user("go"), say("hm"), ...call("Bash", { command: "x" }, null), say("…")]))[0];
    expect(planFold(t, { live: true })).toBeNull();
    const fold = planFold(t, { live: false });
    expect(fold?.folded).toHaveLength(1);
    expect(fold?.reply.kind === "message" && fold.reply.message.plaintext).toBe("…");
  });

  it("counts files changed with the same line diff each row's own +A −R shows", () => {
    const t = segmentTurns(pairToolMessages([
      user("go"),
      ...call("Edit", { file_path: "a.py", old_string: "x\ny\nz", new_string: "x\nY\nz" }),
      say("done"),
    ]))[0];
    // One line changed of three: +1 −1, not +3 −3.
    expect(filesChanged(t.body)).toEqual([{ path: "a.py", added: 1, removed: 1 }]);
  });

  it("leaves a call that failed only in its text out of the files changed", () => {
    const rows = pairToolMessages([
      user("go"),
      ...call("Edit", { file_path: "a.py", old_string: "x", new_string: "y" }, { plaintext: "Error: String to replace not found" }),
    ]);
    expect(filesChanged(rows)).toEqual([]);
  });

  it("does not fold a turn with no reply text, or nothing ahead of it", () => {
    const noReply = segmentTurns(pairToolMessages([user("go"), ...call("Bash", { command: "x" })]))[0];
    expect(planFold(noReply, { live: false })).toBeNull();
    const justReply = segmentTurns(pairToolMessages([user("hi"), say("hello")]))[0];
    expect(planFold(justReply, { live: false })).toBeNull();
  });

  it("keeps rows the person acts on out of the fold", () => {
    const view = { tool_call_id: "c", site: "s", tool: "t", resource_uri: "ui://x" };
    const t = segmentTurns(
      pairToolMessages([
        user("go"),
        ...call("Bash", { command: "x" }),
        ...call("mcp__labs__show", {}, { app: view }),
        ...call("AskUserQuestion", { questions: [] }),
        say("done"),
      ]),
    )[0];
    const fold = planFold(t, { live: false })!;
    expect(fold.folded).toHaveLength(1);
    expect(fold.kept).toHaveLength(2);
    expect(fold.summary.toolCalls).toBe(3);
  });

  it("counts failed calls for the fold line", () => {
    const t = segmentTurns(
      pairToolMessages([user("go"), ...call("Bash", { command: "x" }, { status: "error" }), say("ok")]),
    )[0];
    expect(planFold(t, { live: false })!.summary.errors).toBe(1);
  });
});

describe("filesChanged", () => {
  it("adds up each file's edits, in first-touched order, and skips failed calls", () => {
    const rows = pairToolMessages([
      ...call("Write", { file_path: "new.ts", content: "a\nb\nc\n" }),
      ...call("Edit", { file_path: "old.py", old_string: "x\ny", new_string: "z" }),
      ...call("MultiEdit", { file_path: "old.py", edits: [{ old_string: "q", new_string: "r\ns" }] }),
      ...call("NotebookEdit", { notebook_path: "n.ipynb", new_source: "print(1)" }),
      ...call("Edit", { file_path: "broken.py", old_string: "a", new_string: "b" }, { status: "error" }),
      ...call("Read", { file_path: "read-only.py" }),
    ]);
    expect(filesChanged(rows)).toEqual([
      { path: "new.ts", added: 3, removed: 0 },
      { path: "old.py", added: 3, removed: 3 },
      { path: "n.ipynb", added: 1, removed: 0 },
    ]);
  });

  it("counts lines without a phantom one for a trailing newline", () => {
    expect(countLines("")).toBe(0);
    expect(countLines("a")).toBe(1);
    expect(countLines("a\n")).toBe(1);
    expect(countLines("a\nb")).toBe(2);
    expect(countLines(undefined)).toBe(0);
  });
});

describe("latestTodos", () => {
  it("is the last successful TodoWrite's list", () => {
    const rows = pairToolMessages([
      ...call("TodoWrite", { todos: [{ content: "a", status: "pending" }] }),
      ...call("TodoWrite", { todos: [{ content: "a", status: "completed" }, { content: "b", status: "in_progress", activeForm: "Doing b" }] }),
      ...call("TodoWrite", { todos: [{ content: "lost", status: "pending" }] }, { status: "error" }),
    ]);
    expect(latestTodos(rows)).toEqual([
      { content: "a", status: "completed", activeForm: undefined },
      { content: "b", status: "in_progress", activeForm: "Doing b" },
    ]);
  });

  it("is null for a turn without one", () => {
    expect(latestTodos(pairToolMessages(call("Bash", { command: "ls" })))).toBeNull();
  });
});

describe("currentActivity", () => {
  it("names the newest running call", () => {
    const rows = pairToolMessages([
      ...call("Read", { file_path: "a.py" }),
      ...call("Edit", { file_path: "apps/x.py" }, null),
    ]);
    expect(currentActivity(rows)).toEqual({ verb: "Editing", target: "apps/x.py" });
  });

  it("is null while nothing runs — the turn is thinking", () => {
    expect(currentActivity(pairToolMessages(call("Read", { file_path: "a.py" })))).toBeNull();
  });

  it("describes the common calls in words", () => {
    const use = (name: string, input: Record<string, unknown>) => call(name, input, null)[0];
    expect(describeCall(use("Bash", { command: "npm test\nmore" }))).toEqual({ verb: "Running", target: "npm test" });
    expect(describeCall(use("Grep", { pattern: "foo" }))).toEqual({ verb: "Searching for", target: "foo" });
    expect(describeCall(use("TodoWrite", {}))).toEqual({ verb: "Updating the to-do list" });
    expect(describeCall(use("mcp__connect_labs__data_query", {}))).toEqual({ verb: "Calling Labs:", target: "data_query" });
  });
});

describe("formatElapsed", () => {
  it("reads as a clock", () => {
    expect(formatElapsed(900)).toBe("0s");
    expect(formatElapsed(12_000)).toBe("12s");
    expect(formatElapsed(72_000)).toBe("1m 12s");
    expect(formatElapsed(65 * 60_000)).toBe("1h 5m");
  });
});
