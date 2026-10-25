import { describe, expect, it } from "vitest";

import { diffStats, editStats, fileEdits, lineDiff } from "./diff";

describe("lineDiff", () => {
  it("keeps shared lines as context and marks the changed one", () => {
    expect(lineDiff("a\nb\nc", "a\nB\nc")).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "del", text: "b" },
      { kind: "add", text: "B" },
      { kind: "ctx", text: "c" },
    ]);
  });

  it("finds an inserted line in the middle without re-adding its neighbours", () => {
    const d = lineDiff("x\ny\nz", "x\ny\nnew\nz");
    expect(diffStats(d)).toEqual({ added: 1, removed: 0 });
    expect(d.filter((l) => l.kind === "add")).toEqual([{ kind: "add", text: "new" }]);
  });

  it("lines up interleaved changes by longest common subsequence", () => {
    const d = lineDiff("a\nb\nc\nd", "a\nX\nc\nY");
    expect(d.map((l) => `${l.kind}:${l.text}`)).toEqual([
      "ctx:a", "del:b", "add:X", "ctx:c", "del:d", "add:Y",
    ]);
  });

  it("treats a trailing newline as the end of the last line", () => {
    expect(lineDiff("", "one\ntwo\n")).toEqual([
      { kind: "add", text: "one" },
      { kind: "add", text: "two" },
    ]);
  });

  it("is empty of changes when nothing changed", () => {
    expect(diffStats(lineDiff("same", "same"))).toEqual({ added: 0, removed: 0 });
  });
});

describe("fileEdits / editStats", () => {
  it("reads one Edit, noting replace_all", () => {
    expect(
      fileEdits("Edit", { file_path: "/a", old_string: "x", new_string: "y", replace_all: true }),
    ).toEqual([{ oldString: "x", newString: "y", replaceAll: true }]);
  });

  it("sums every edit of a MultiEdit", () => {
    const stats = editStats("MultiEdit", {
      file_path: "/a",
      edits: [
        { old_string: "a", new_string: "b\nc" },
        { old_string: "d\ne", new_string: "" },
      ],
    });
    expect(stats).toEqual({ added: 2, removed: 3 });
  });

  it("counts a Write as all-added lines", () => {
    expect(editStats("Write", { file_path: "/a", content: "1\n2\n3\n" })).toEqual({
      added: 3,
      removed: 0,
    });
  });

  it("is null for any other tool or a malformed input", () => {
    expect(editStats("Bash", { command: "ls" })).toBeNull();
    expect(editStats("Edit", "nope")).toBeNull();
    expect(editStats("MultiEdit", { edits: "x" })).toBeNull();
  });
});
