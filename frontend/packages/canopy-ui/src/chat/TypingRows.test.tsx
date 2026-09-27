// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { TypingRows } from "./TypingRows";
import type { PeerDraft } from "./protocol";

afterEach(cleanup);

describe("TypingRows", () => {
  it("renders nothing when nobody is typing", () => {
    const { container } = render(<TypingRows peers={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("shows a peer's live words when a body is present", () => {
    const peers: PeerDraft[] = [{ author: { id: 2, name: "Bo B" }, body: "hello there", at: null }];
    render(<TypingRows peers={peers} />);
    const row = screen.getByTestId("typing-row");
    expect(row.textContent).toContain("Bo B");
    expect(row.textContent).toContain("hello there");
    expect(row.textContent).toContain("is typing:");
  });

  it("renders 'is typing…' with no words when the body is withheld", () => {
    const peers: PeerDraft[] = [{ author: { id: 2, name: "Bo B" }, body: "", at: null, typing: true }];
    render(<TypingRows peers={peers} />);
    const row = screen.getByTestId("typing-row");
    expect(row.textContent).toContain("Bo B");
    expect(row.textContent).toContain("is typing…");
    expect(row.textContent).not.toContain("is typing:");
  });
});
