// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { Message } from "./protocol";
import { ToolCallPair } from "./ToolCallPair";
import { DIFF_LINE_CAP } from "./toolBodies/ToolBody";

function row(partial: Partial<Message> & Pick<Message, "role">): Message {
  return {
    id: "1", turn_index: 1, content: {}, plaintext: "", status: "complete",
    error_detail: null, started_at: null, completed_at: null,
    created_at: "2026-10-24T00:00:00Z", ...partial,
  };
}
const use = (name: string, input: unknown) =>
  row({ role: "tool_use", content: { id: "t1", name, input } });
const result = (plaintext: string, status: Message["status"] = "complete") =>
  row({ id: "2", role: "tool_result", content: { tool_use_id: "t1" }, plaintext, status });

afterEach(() => cleanup());

describe("ToolCallPair — file edits", () => {
  it("shows an Edit as a diff with +A −R in the header, not JSON", () => {
    const { container } = render(
      <ToolCallPair
        use={use("Edit", { file_path: "apps/x.py", old_string: "a = 1\nb = 2", new_string: "a = 1\nb = 3\nc = 4" })}
        result={result("The file apps/x.py has been updated successfully.")}
      />,
    );
    expect(screen.getByTestId("tool-diff-stats").textContent).toBe("+2 −1");
    expect(container.querySelectorAll('[data-diff="del"]')).toHaveLength(1);
    expect(container.querySelectorAll('[data-diff="add"]')).toHaveLength(2);
    expect(container.textContent).not.toContain('"old_string"');
    // The boilerplate success line is not repeated under the diff.
    expect(container.textContent).not.toContain("updated successfully");
  });

  it("notes replace_all and numbers each edit of a MultiEdit", () => {
    render(
      <ToolCallPair
        use={use("MultiEdit", {
          file_path: "/f.ts",
          edits: [
            { old_string: "x", new_string: "y", replace_all: true },
            { old_string: "p", new_string: "q" },
          ],
        })}
        result={result("ok")}
      />,
    );
    const body = screen.getByTestId("tool-diff").textContent ?? "";
    expect(body).toContain("edit 1 of 2");
    expect(body).toContain("replace all occurrences");
    expect(body).toContain("edit 2 of 2");
    expect(screen.getByTestId("tool-diff-stats").textContent).toBe("+2 −2");
  });

  it("caps a long Write behind 'show all N lines'", () => {
    const content = Array.from({ length: 100 }, (_, i) => `line ${i}`).join("\n");
    const { container } = render(
      <ToolCallPair use={use("Write", { file_path: "/big.txt", content })} result={result("ok")} />,
    );
    expect(container.querySelectorAll('[data-diff="add"]')).toHaveLength(DIFF_LINE_CAP);
    fireEvent.click(screen.getByText("show all 100 lines"));
    expect(container.querySelectorAll('[data-diff="add"]')).toHaveLength(100);
    expect(screen.getByTestId("tool-diff-stats").textContent).toBe("+100 −0");
  });

  it("still shows the result when an edit failed", () => {
    render(
      <ToolCallPair
        use={use("Edit", { file_path: "/f", old_string: "x", new_string: "y" })}
        result={result("String to replace not found in file.", "error")}
      />,
    );
    expect(screen.getByTestId("tool-result").getAttribute("data-error")).toBe("true");
  });
});

describe("ToolCallPair — Bash", () => {
  it("shows the description, $ command and output", () => {
    render(
      <ToolCallPair
        use={use("Bash", { command: "ls -la", description: "List files" })}
        result={result("total 0")}
      />,
    );
    const body = screen.getByTestId("tool-bash").textContent ?? "";
    expect(body).toContain("List files");
    expect(body).toContain("$ ls -la");
    expect(body).toContain("total 0");
    expect(body).not.toContain('"command"');
    expect(screen.getByTestId("tool-result").hasAttribute("data-error")).toBe(false);
  });

  it("styles a failed command's output as an error", () => {
    render(<ToolCallPair use={use("Bash", { command: "false" })} result={result("boom", "error")} />);
    expect(screen.getByTestId("tool-result").getAttribute("data-error")).toBe("true");
  });
});

describe("ToolCallPair — key/value inputs", () => {
  it("lists Read's fields instead of a JSON dump", () => {
    render(<ToolCallPair use={use("Read", { file_path: "/a/b.py", limit: 20 })} result={result("1\tx")} />);
    const fields = screen.getByTestId("tool-input-fields").textContent ?? "";
    expect(fields).toContain("file_path");
    expect(fields).toContain("/a/b.py");
    expect(fields).toContain("limit");
    expect(fields).not.toContain("{");
  });

  it("lists an mcp__ tool's fields", () => {
    render(
      <ToolCallPair
        use={use("mcp__connect_labs__data_query", { sql: "select 1", opportunity_id: 7 })}
        result={result("[]")}
      />,
    );
    expect(screen.getByTestId("tool-input-fields").textContent).toContain("opportunity_id7");
  });

  it("falls back to JSON for a tool nothing knows", () => {
    const { container } = render(
      <ToolCallPair use={use("SomethingNew", { a: 1 })} result={result("ok")} />,
    );
    expect(screen.queryByTestId("tool-input-fields")).toBeNull();
    expect(container.textContent).toContain('"a": 1');
  });
});
