/**
 * The `/` menu: typing `/` offers the host's commands, filtered by what follows;
 * Enter or Tab completes the name instead of sending, Esc closes the menu
 * without stopping a running reply.
 */
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { SendBox } from "./SendBox";
import type { Draft } from "./protocol";
import { matchSlashCommands, slashQuery, type SlashCommand } from "./slashCommands";

afterEach(cleanup);

const COMMANDS: SlashCommand[] = [
  { name: "hal:turn", description: "Do a turn.", args_hint: "scope (optional)", source: "agent" },
  { name: "hal:setup", description: "Set up.", source: "plugin" },
  { name: "browse", description: "Drive a browser.", source: "user" },
  { name: "canopy:shareout", description: "Write a shareout.", source: "plugin" },
];

const draft = { id: "d1", body: "", version: 1, last_editor: 1, last_edit_at: "" } as Draft;

function setup(props: Partial<Parameters<typeof SendBox>[0]> = {}) {
  const onSend = vi.fn();
  const onStop = vi.fn();
  render(
    <SendBox
      draft={draft}
      connected
      isStreaming={false}
      streamingMessageId={null}
      onUpdate={vi.fn()}
      onSend={onSend}
      onStop={onStop}
      slashCommands={COMMANDS}
      {...props}
    />,
  );
  const box = () => screen.getByTestId("composer") as HTMLTextAreaElement;
  const options = () => screen.queryAllByRole("option").map((o) => o.textContent ?? "");
  return { box, options, onSend, onStop };
}

describe("matchSlashCommands", () => {
  it("ranks a name prefix, then a prefix after a colon, then a substring", () => {
    const names = (q: string) => matchSlashCommands(COMMANDS, q).map((c) => c.name);
    expect(names("hal")).toEqual(["hal:turn", "hal:setup"]);
    expect(names("s")).toEqual(["hal:setup", "canopy:shareout", "browse"]);
    expect(names("")).toEqual(COMMANDS.map((c) => c.name));
  });

  it("opens only on a leading slash with nothing after the name", () => {
    expect(slashQuery("/ha")).toBe("ha");
    expect(slashQuery("/hal:turn now")).toBeNull();
    expect(slashQuery("say /hal")).toBeNull();
  });
});

describe("SendBox — slash commands", () => {
  it("offers the commands when the message starts with a slash", () => {
    const { box, options } = setup();
    expect(screen.queryByTestId("slash-menu")).toBeNull();
    fireEvent.change(box(), { target: { value: "/" } });
    expect(options()).toHaveLength(4);
    fireEvent.change(box(), { target: { value: "/tu" } });
    // `turn` after the colon first; "se·tu·p" only contains it.
    expect(options()).toHaveLength(2);
    expect(options()[0]).toContain("/hal:turn");
  });

  it("Enter completes the highlighted command instead of sending", () => {
    const { box, onSend } = setup();
    fireEvent.change(box(), { target: { value: "/hal" } });
    fireEvent.keyDown(box(), { key: "ArrowDown" });
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(box().value).toBe("/hal:setup ");
    expect(screen.queryByTestId("slash-menu")).toBeNull();
  });

  it("shows what a completed command takes", () => {
    const { box } = setup();
    fireEvent.change(box(), { target: { value: "/hal:t" } });
    fireEvent.keyDown(box(), { key: "Tab" });
    expect(screen.getByTestId("slash-args-hint").textContent).toContain("scope (optional)");
  });

  it("Esc closes the menu and does not stop the reply", () => {
    const { box, onStop } = setup({ isStreaming: true });
    fireEvent.change(box(), { target: { value: "/b" } });
    fireEvent.keyDown(box(), { key: "Escape" });
    expect(screen.queryByTestId("slash-menu")).toBeNull();
    expect(onStop).not.toHaveBeenCalled();
    fireEvent.change(box(), { target: { value: "/br" } });
    expect(screen.queryByTestId("slash-menu")).not.toBeNull();
  });

  it("a click picks a command", () => {
    const { box, options } = setup();
    fireEvent.change(box(), { target: { value: "/" } });
    expect(options()).toHaveLength(4);
    fireEvent.mouseDown(screen.getAllByRole("option")[2]);
    expect(box().value).toBe("/browse ");
  });

  it("without commands a slash is just text", () => {
    const { box, onSend } = setup({ slashCommands: undefined });
    fireEvent.change(box(), { target: { value: "/x" } });
    expect(screen.queryByTestId("slash-menu")).toBeNull();
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(onSend).toHaveBeenCalled();
  });
});
