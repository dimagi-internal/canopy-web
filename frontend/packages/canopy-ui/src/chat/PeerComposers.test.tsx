// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { PeerComposers } from "./PeerComposers";
import { TypingRows } from "./TypingRows";
import { personColor } from "./personColor";
import type { PeerDraft } from "./protocol";

afterEach(cleanup);

const bo: PeerDraft = { author: { id: 2, name: "Bo Banda" }, body: "hello there", at: null };

describe("PeerComposers", () => {
  it("renders nothing when nobody is typing", () => {
    const { container } = render(<PeerComposers peers={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("draws a box with the person's avatar, name and live words", () => {
    render(<PeerComposers peers={[bo]} />);
    const box = screen.getByTestId("peer-composer");
    // The name is always shown — colour is never the only cue.
    expect(box.textContent).toContain("Bo Banda is typing");
    expect(box.textContent).toContain("BB");
    expect(screen.getByTestId("peer-composer-text").textContent).toContain("hello there");
    expect(box.className).toContain(personColor(2).edge);
  });

  it("ends the live words with a caret that only blinks when motion is allowed", () => {
    render(<PeerComposers peers={[bo]} />);
    const caret = screen.getByTestId("peer-caret");
    expect(caret.getAttribute("aria-hidden")).toBe("true");
    expect(caret.className).toContain("motion-safe:animate-pulse");
    expect(caret.className).not.toMatch(/(^|\s)animate-/);
  });

  it("wraps the words instead of truncating them to one line", () => {
    render(<PeerComposers peers={[bo]} />);
    const text = screen.getByTestId("peer-composer-text");
    expect(text.className).toContain("whitespace-pre-wrap");
    expect(text.className).not.toContain("truncate");
  });

  it("shows a typing indicator, not words, when the body is withheld", () => {
    render(<PeerComposers peers={[{ ...bo, body: "", typing: true }]} />);
    expect(screen.getByTestId("peer-composer").textContent).toContain("Bo Banda is typing");
    expect(screen.getByTestId("peer-typing-indicator")).toBeTruthy();
    expect(screen.queryByTestId("peer-composer-text")).toBeNull();
  });

  it("stacks several people newest-edit-last and stays a polite live region", () => {
    const robin: PeerDraft = { author: { id: 3, name: "Robin Sharma" }, body: "and me", at: null };
    render(<PeerComposers peers={[bo, robin]} />);
    const boxes = screen.getAllByTestId("peer-composer");
    expect(boxes.map((b) => b.getAttribute("data-user-id"))).toEqual(["2", "3"]);
    expect(screen.getByTestId("peer-composers").getAttribute("aria-live")).toBe("polite");
  });

  it("keeps the deprecated TypingRows export rendering the same boxes", () => {
    render(<TypingRows peers={[bo]} />);
    expect(screen.getByTestId("peer-composer").textContent).toContain("hello there");
  });
});
