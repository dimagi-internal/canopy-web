/**
 * The composer is LOCAL-FIRST: what you are typing is local component state,
 * never server state.
 *
 * It used to render `value={draft.body}` straight off the websocket reducer, so
 * every inbound frame was a chance to overwrite the user mid-keystroke — and in
 * SINGLE-PLAYER that trade bought nothing, because there is no co-editor to
 * reconcile with. Three ways it went wrong, all reproduced below:
 *   * `session.state` replaces state wholesale on every reconnect, reverting
 *     anything typed since the last 150ms flush;
 *   * a stale echo of your OWN debounced update rewound the textarea;
 *   * two clients on one account (phone + desktop, which this app encourages)
 *     fight over the draft version until a mismatch clears the pending body.
 */
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";

import { SendBox } from "./SendBox";
import type { Draft } from "./protocol";

afterEach(cleanup);

const ME = 1;
const THEM = 2;

function draft(overrides: Partial<Draft> = {}): Draft {
  return {
    id: "d1",
    body: "",
    version: 1,
    last_editor: ME,
    last_edit_at: new Date().toISOString(),
    ...overrides,
  } as Draft;
}

function setup(props: Partial<Parameters<typeof SendBox>[0]> = {}) {
  const onUpdate = vi.fn();
  const onSend = vi.fn();
  const view = render(
    <SendBox
      draft={draft()}
      connected
      currentUserId={ME}
      holderIsPresent={false}
      isStreaming={false}
      streamingMessageId={null}
      onUpdate={onUpdate}
      onSend={onSend}
      onStop={vi.fn()}
      onTakeOver={vi.fn()}
      {...props}
    />,
  );
  const textarea = () => screen.getByRole("textbox") as HTMLTextAreaElement;
  return { ...view, textarea, onUpdate, onSend };
}

describe("SendBox — local-first composer", () => {
  it("keeps what you typed when a stale echo of your own draft arrives", () => {
    const { textarea, rerender } = setup();
    fireEvent.change(textarea(), { target: { value: "hello wor" } });

    // The server echoes the previous debounced update back at us.
    rerender(
      <SendBox
        draft={draft({ body: "hel", version: 2, last_editor: ME })}
        connected
        currentUserId={ME}
        holderIsPresent={false}
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
        onTakeOver={vi.fn()}
      />,
    );

    expect(textarea().value).toBe("hello wor");
  });

  it("survives a reconnect snapshot carrying a stale body", () => {
    // session.state replaces the whole state object, so this is exactly what a
    // reconnect looked like: everything typed since the last flush, gone.
    const { textarea, rerender } = setup();
    fireEvent.change(textarea(), { target: { value: "a long message" } });

    rerender(
      <SendBox
        draft={draft({ id: "d1", body: "a long", version: 9, last_editor: ME })}
        connected
        currentUserId={ME}
        holderIsPresent={false}
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
        onTakeOver={vi.fn()}
      />,
    );

    expect(textarea().value).toBe("a long message");
  });

  it("DOES adopt an edit made by someone else", () => {
    // Multiplayer still works: a teammate's edit is the one case where the
    // server genuinely knows better than this client.
    const { textarea, rerender } = setup();

    rerender(
      <SendBox
        draft={draft({ body: "from my teammate", version: 3, last_editor: THEM })}
        connected
        currentUserId={ME}
        holderIsPresent
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
        onTakeOver={vi.fn()}
      />,
    );

    expect(textarea().value).toBe("from my teammate");
  });

  it("still reports every keystroke upstream", () => {
    // Local-first governs what is DISPLAYED; the hook still needs the body so
    // it can sync (multiplayer) and flush before chat.send commits the
    // server-side draft.
    const { textarea, onUpdate } = setup();
    fireEvent.change(textarea(), { target: { value: "hi" } });
    expect(onUpdate).toHaveBeenCalledWith("hi");
  });

  it("lets you type before the draft has arrived", () => {
    // The textarea used to be disabled until session.state landed, so every
    // reconnect locked you out of your own composer.
    const { textarea } = setup({ draft: null });
    expect(textarea().disabled).toBe(false);

    fireEvent.change(textarea(), { target: { value: "typed while connecting" } });
    expect(textarea().value).toBe("typed while connecting");
  });

  it("cannot send until a draft exists, since chat.send commits the server copy", () => {
    const { textarea } = setup({ draft: null });
    fireEvent.change(textarea(), { target: { value: "hi" } });
    expect(screen.getByRole("button", { name: /send/i }).hasAttribute("disabled")).toBe(true);
  });

  it("clears the composer when you send", () => {
    const { textarea, onSend } = setup();
    fireEvent.change(textarea(), { target: { value: "ship it" } });

    fireEvent.click(screen.getByRole("button", { name: /send/i }));

    expect(onSend).toHaveBeenCalled();
    expect(textarea().value).toBe("");
  });

  it("still blocks editing while a teammate holds the draft", () => {
    const { textarea } = setup({
      draft: draft({ last_editor: THEM, body: "theirs" }),
      holderIsPresent: true,
    });
    expect(textarea().disabled).toBe(true);
  });
});

describe("SendBox — sending is gated on the socket", () => {
  it("keeps your text instead of clearing it when the socket is down", () => {
    // The composer clears optimistically, and the hook's send() silently drops
    // every frame but chat.stop while closed — so an allowed send here would
    // empty the box and lose the message.
    const { textarea, onSend } = setup({ connected: false });
    fireEvent.change(textarea(), { target: { value: "do not lose me" } });

    fireEvent.click(screen.getByRole("button", { name: /send/i }));

    expect(onSend).not.toHaveBeenCalled();
    expect(textarea().value).toBe("do not lose me");
  });

  it("still lets you type while disconnected", () => {
    const { textarea } = setup({ connected: false });
    fireEvent.change(textarea(), { target: { value: "composed offline" } });
    expect(textarea().disabled).toBe(false);
    expect(textarea().value).toBe("composed offline");
  });
});

describe("SendBox — cancelling a queued turn", () => {
  it("offers stop while a send is outstanding, before any reply exists", () => {
    // The gap this closes: between send and the first token there is NO
    // assistant message, so `inFlightMessage` is null and the stop button never
    // rendered — exactly the window where you most want out, because a queued
    // turn means no runner has picked it up (offline, or busy with another).
    // The server has always handled it: chat.stop cancels every non-terminal
    // turn and ignores message_id.
    const onStop = vi.fn();
    render(
      <SendBox
        draft={draft()}
        connected
        currentUserId={ME}
        holderIsPresent={false}
        isStreaming
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={onStop}
        onTakeOver={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /stop/i }));

    expect(onStop).toHaveBeenCalledWith(null);
  });

  it("passes the message id through once a reply is streaming", () => {
    const onStop = vi.fn();
    render(
      <SendBox
        draft={draft()}
        connected
        currentUserId={ME}
        holderIsPresent={false}
        isStreaming
        streamingMessageId="m1"
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={onStop}
        onTakeOver={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /stop/i }));

    expect(onStop).toHaveBeenCalledWith("m1");
  });
});
