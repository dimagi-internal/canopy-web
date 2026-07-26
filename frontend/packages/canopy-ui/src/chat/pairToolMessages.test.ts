import { describe, expect, it } from "vitest";

import type { Message } from "./protocol";
import {
  deriveToolStatus,
  pairToolMessages,
  toolDisplayName,
  toolPreview,
} from "./pairToolMessages";

let seq = 0;
function msg(
  partial: Partial<Message> & { id: string; role: Message["role"] },
): Message {
  return {
    turn_index: seq++,
    content: {},
    plaintext: "",
    status: "complete",
    error_detail: null,
    started_at: null,
    completed_at: null,
    created_at: "2026-05-13T00:00:00Z",
    ...partial,
  };
}

describe("pairToolMessages", () => {
  it("pairs consecutive tool_use + tool_result by id/tool_use_id", () => {
    const rows = pairToolMessages([
      msg({ id: "1", role: "user", plaintext: "hi" }),
      msg({
        id: "2",
        role: "tool_use",
        content: { id: "t1", name: "Bash", input: { command: "ls" } },
      }),
      msg({
        id: "3",
        role: "tool_result",
        content: { tool_use_id: "t1" },
        plaintext: "out",
      }),
      msg({ id: "4", role: "assistant", plaintext: "done" }),
    ]);
    expect(rows).toHaveLength(3);
    expect(rows[0].kind).toBe("message");
    expect(rows[1].kind).toBe("tool_pair");
    if (rows[1].kind === "tool_pair") {
      expect(rows[1].use.id).toBe("2");
      expect(rows[1].result?.id).toBe("3");
    }
    expect(rows[2].kind).toBe("message");
  });

  it("pairs tool_use with a later non-adjacent tool_result", () => {
    const rows = pairToolMessages([
      msg({ id: "1", role: "tool_use", content: { id: "a", name: "X" } }),
      msg({ id: "2", role: "tool_use", content: { id: "b", name: "Y" } }),
      msg({ id: "3", role: "tool_result", content: { tool_use_id: "a" } }),
      msg({ id: "4", role: "tool_result", content: { tool_use_id: "b" } }),
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0].kind).toBe("tool_pair");
    expect(rows[1].kind).toBe("tool_pair");
    if (rows[0].kind === "tool_pair" && rows[1].kind === "tool_pair") {
      expect(rows[0].use.id).toBe("1");
      expect(rows[0].result?.id).toBe("3");
      expect(rows[1].use.id).toBe("2");
      expect(rows[1].result?.id).toBe("4");
    }
  });

  it("leaves an in-flight tool_use with null result so UI can show pending", () => {
    const rows = pairToolMessages([
      msg({ id: "1", role: "tool_use", content: { id: "t1", name: "Bash" } }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("tool_pair");
    if (rows[0].kind === "tool_pair") {
      expect(rows[0].result).toBeNull();
    }
  });

  it("falls back to standalone when a tool_result has no matching use", () => {
    const rows = pairToolMessages([
      msg({ id: "1", role: "tool_result", content: { tool_use_id: "ghost" } }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("message");
  });

  it("doesn't lose tool calls in long sessions", () => {
    const msgs: Message[] = [];
    for (let i = 0; i < 44; i++) {
      msgs.push(
        msg({
          id: `${i * 2 + 1}`,
          role: "tool_use",
          content: { id: `t${i}`, name: "Bash" },
        }),
      );
      if (i < 42) {
        msgs.push(
          msg({
            id: `${i * 2 + 2}`,
            role: "tool_result",
            content: { tool_use_id: `t${i}` },
          }),
        );
      }
    }
    const rows = pairToolMessages(msgs);
    expect(rows).toHaveLength(44);
    expect(rows.filter((r) => r.kind === "tool_pair")).toHaveLength(44);
    // First 42 paired, last 2 pending.
    const pending = rows.filter(
      (r) => r.kind === "tool_pair" && r.result === null,
    );
    expect(pending).toHaveLength(2);
  });

  // The case this feature exists for: two tool calls in flight at once (Claude
  // does this routinely). Their tool_start events land back-to-back, both
  // still open, before either tool_end arrives — a flat order-based pairing
  // would have no way to tell which result belongs to which call. The
  // results also arrive OUT OF ORDER (second call finishes first), which
  // order-based pairing would get flatly wrong.
  it("pairs overlapping (parallel) tool calls by id, including out-of-order results", () => {
    const rows = pairToolMessages([
      msg({
        id: "1", role: "tool_use",
        content: { id: "call-a", name: "Bash", input: { command: "sleep 10" } },
      }),
      msg({
        id: "2", role: "tool_use",
        content: { id: "call-b", name: "Read", input: { file_path: "/x" } },
      }),
      // call-b finishes first even though call-a started first.
      msg({ id: "3", role: "tool_result", content: { tool_use_id: "call-b" }, plaintext: "file contents" }),
      msg({ id: "4", role: "tool_result", content: { tool_use_id: "call-a" }, plaintext: "done sleeping" }),
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0].kind).toBe("tool_pair");
    expect(rows[1].kind).toBe("tool_pair");
    if (rows[0].kind === "tool_pair" && rows[1].kind === "tool_pair") {
      expect(rows[0].use.id).toBe("1");
      expect(rows[0].result?.id).toBe("4"); // call-a's result, not the earlier-arriving one
      expect(rows[1].use.id).toBe("2");
      expect(rows[1].result?.id).toBe("3"); // call-b's result
    }
  });

  // Two overlapping calls to the SAME tool name — order/name matching alone
  // is ambiguous here (that's the whole point of the id); only tool_use_id
  // disambiguates which result goes with which call.
  it("pairs two overlapping calls with the same tool name by id", () => {
    const rows = pairToolMessages([
      msg({
        id: "1", role: "tool_use",
        content: { id: "call-1", name: "Bash", input: { command: "echo one" } },
      }),
      msg({
        id: "2", role: "tool_use",
        content: { id: "call-2", name: "Bash", input: { command: "echo two" } },
      }),
      msg({ id: "3", role: "tool_result", content: { tool_use_id: "call-2" }, plaintext: "two" }),
      msg({ id: "4", role: "tool_result", content: { tool_use_id: "call-1" }, plaintext: "one" }),
    ]);
    expect(rows).toHaveLength(2);
    if (rows[0].kind === "tool_pair" && rows[1].kind === "tool_pair") {
      expect(rows[0].use.id).toBe("1");
      expect(rows[0].result?.id).toBe("4");
      expect(rows[0].result?.plaintext).toBe("one");
      expect(rows[1].use.id).toBe("2");
      expect(rows[1].result?.id).toBe("3");
      expect(rows[1].result?.plaintext).toBe("two");
    }
  });

  // Backward compatibility: events with no correlation id at all (older
  // ledger rows, or a runner that hasn't been updated yet) must still pair —
  // falling back to the pre-id FIFO order heuristic.
  it("falls back to FIFO order pairing when neither side carries an id", () => {
    const rows = pairToolMessages([
      msg({ id: "1", role: "tool_use", content: { name: "Bash" } }),
      msg({ id: "2", role: "tool_use", content: { name: "Read" } }),
      msg({ id: "3", role: "tool_result", plaintext: "first result" }),
      msg({ id: "4", role: "tool_result", plaintext: "second result" }),
    ]);
    expect(rows).toHaveLength(2);
    if (rows[0].kind === "tool_pair" && rows[1].kind === "tool_pair") {
      expect(rows[0].use.id).toBe("1");
      expect(rows[0].result?.id).toBe("3");
      expect(rows[1].use.id).toBe("2");
      expect(rows[1].result?.id).toBe("4");
    }
  });

  // The real production shape: a session whose earlier turns predate the
  // tool_use_id field (no ids) followed by a later turn from an updated
  // producer (has ids) — both must pair correctly, and the id-less fallback
  // queue must not accidentally absorb an id-tagged result or vice versa.
  it("pairs correctly across a mixed stream (legacy no-id turn followed by an id-tagged turn)", () => {
    const rows = pairToolMessages([
      // Legacy turn: no ids at all.
      msg({ id: "1", role: "tool_use", content: { name: "Bash" } }),
      msg({ id: "2", role: "tool_result", plaintext: "legacy result" }),
      // Newer turn: two parallel calls, correlated by id.
      msg({ id: "3", role: "tool_use", content: { id: "new-a", name: "Bash" } }),
      msg({ id: "4", role: "tool_use", content: { id: "new-b", name: "Bash" } }),
      msg({ id: "5", role: "tool_result", content: { tool_use_id: "new-b" }, plaintext: "b done" }),
      msg({ id: "6", role: "tool_result", content: { tool_use_id: "new-a" }, plaintext: "a done" }),
    ]);
    expect(rows).toHaveLength(3);
    expect(rows.every((r) => r.kind === "tool_pair")).toBe(true);
    if (rows[0].kind === "tool_pair" && rows[1].kind === "tool_pair" && rows[2].kind === "tool_pair") {
      expect(rows[0].use.id).toBe("1");
      expect(rows[0].result?.id).toBe("2");
      expect(rows[1].use.id).toBe("3");
      expect(rows[1].result?.id).toBe("6"); // new-a's result
      expect(rows[2].use.id).toBe("4");
      expect(rows[2].result?.id).toBe("5"); // new-b's result
    }
  });
});

describe("deriveToolStatus", () => {
  it("returns pending when result is missing", () => {
    const use = msg({ id: "1", role: "tool_use" });
    expect(deriveToolStatus(use, null).kind).toBe("pending");
  });

  it("returns error when result is in error status", () => {
    const use = msg({ id: "1", role: "tool_use" });
    const result = msg({ id: "2", role: "tool_result", status: "error" });
    expect(deriveToolStatus(use, result).kind).toBe("error");
  });

  it("sniffs error-like result content", () => {
    const use = msg({ id: "1", role: "tool_use" });
    const result = msg({
      id: "2",
      role: "tool_result",
      plaintext: "Error: file not found",
    });
    expect(deriveToolStatus(use, result).kind).toBe("error");
  });

  it("returns success on a clean result", () => {
    const use = msg({ id: "1", role: "tool_use" });
    const result = msg({ id: "2", role: "tool_result", plaintext: "ok" });
    expect(deriveToolStatus(use, result).kind).toBe("success");
  });
});

describe("toolPreview / toolDisplayName", () => {
  it("shows the first line of a Bash command", () => {
    const use = msg({
      id: "1",
      role: "tool_use",
      content: {
        name: "Bash",
        input: { command: "ls -la /tmp\n# more after newline" },
      },
    });
    expect(toolPreview(use, null)).toBe("ls -la /tmp");
  });

  it("shows file_path for Read/Write/Edit", () => {
    for (const name of ["Read", "Write", "Edit"]) {
      const use = msg({
        id: "1",
        role: "tool_use",
        content: { name, input: { file_path: "/a/b.txt" } },
      });
      expect(toolPreview(use, null)).toBe("/a/b.txt");
    }
  });

  it("shows the todo count for TodoWrite", () => {
    const use = msg({
      id: "1",
      role: "tool_use",
      content: {
        name: "TodoWrite",
        input: { todos: [{}, {}, {}] },
      },
    });
    expect(toolPreview(use, null)).toBe("3 todos");
  });

  it("shows the skill name for Skill", () => {
    const use = msg({
      id: "1",
      role: "tool_use",
      content: { name: "Skill", input: { skill: "ace:run" } },
    });
    expect(toolPreview(use, null)).toBe("ace:run");
  });

  it("strips the mcp prefix for MCP tools", () => {
    const use = msg({
      id: "1",
      role: "tool_use",
      content: {
        name: "mcp__plugin_ace_ace-gdrive__drive_create_file",
        input: {},
      },
    });
    expect(toolDisplayName(use)).toBe("drive_create_file");
  });
});
