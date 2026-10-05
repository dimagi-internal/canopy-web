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
      isStreaming={false}
      streamingMessageId={null}
      onUpdate={onUpdate}
      onSend={onSend}
      onStop={vi.fn()}
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
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
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
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    expect(textarea().value).toBe("a long message");
  });

  it("never adopts another editor's body — this box is only ever your own draft now", () => {
    // Everyone gets their own draft (`SessionState.active_draft` is the
    // CALLER's own); a teammate's live text arrives via `peer_drafts` /
    // <PeerComposers> instead, never through this component's `draft` prop. So a
    // `last_editor` that isn't you must NOT overwrite what you're typing.
    const { textarea, rerender } = setup();
    fireEvent.change(textarea(), { target: { value: "my own words" } });

    rerender(
      <SendBox
        draft={draft({ body: "from my teammate", version: 3, last_editor: THEM })}
        connected
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    );

    expect(textarea().value).toBe("my own words");
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

  it("never locks editing for a teammate's draft — this box is never theirs to hold", () => {
    // There is no shared lock any more: every editor has their own draft, so a
    // `last_editor` naming someone else describes no state this box renders.
    const { textarea } = setup({
      draft: draft({ last_editor: THEM, body: "theirs" }),
    });
    expect(textarea().disabled).toBe(false);
  });
});

describe("SendBox — a local draft (contact / widget) restores a failed send", () => {
  // A contact's or the widget's own composer has no server draft to co-edit;
  // `useSessionSocket` stands in a LOCAL one (`id: "local"`) instead. Sending
  // clears `localBody` optimistically (see "clears the composer when you
  // send" above) before the round trip even starts — so a failure has no way
  // back into the box except through this draft, once the hook re-seeds its
  // body in the catch handler (`sendChatOverHttp`).
  function localDraft(body: string): Draft {
    return {
      id: "local", slot: "next", status: "open", body, version: 0,
      last_editor: 0, last_edit_at: new Date().toISOString(),
    } as Draft;
  }

  it("shows the text again once the local draft's body is restored after a send", () => {
    const { textarea, rerender } = setup({ draft: localDraft("hello") });
    expect(textarea().value).toBe("hello");

    // Pressing Send clears the box optimistically — `handleSend`'s own
    // `setLocalBody("")`, independent of whatever the draft prop says.
    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
    expect(textarea().value).toBe("");

    // The send already went out — the hook cleared its local draft to null the
    // instant it fired, well before any round trip could resolve.
    rerender(
      <SendBox
        draft={null}
        connected
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    expect(textarea().value).toBe("");

    // The HTTP send failed; the hook re-seeds the SAME local draft with the
    // original text (`updateLocalDraft` in its catch handler).
    rerender(
      <SendBox
        draft={localDraft("hello")}
        connected
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    expect(textarea().value).toBe("hello");
  });

  it("does not restore a real multiplayer draft the same way", () => {
    // The mirror is scoped to `id === "local"` on purpose: a server-assigned
    // id is a real co-editor's draft, and adopting it unconditionally is
    // exactly the deleted `theirEdit` bug (it could overwrite live typing).
    const { textarea, rerender } = setup({ draft: null });
    rerender(
      <SendBox
        draft={draft({ id: "d1", body: "not mine to adopt", last_editor: THEM })}
        connected
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    expect(textarea().value).toBe("");
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
        isStreaming
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={onStop}
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
        isStreaming
        streamingMessageId="m1"
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={onStop}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /stop/i }));

    expect(onStop).toHaveBeenCalledWith("m1");
  });

  it("stays available to send while the agent is replying", () => {
    // The composer used to lock Send for the whole reply; a send made while
    // streaming is QUEUED server-side now, not refused client-side.
    const onSend = vi.fn();
    render(
      <SendBox
        draft={draft()}
        connected
        isStreaming
        streamingMessageId="m1"
        onUpdate={vi.fn()}
        onSend={onSend}
        onStop={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "queue me up" } });

    expect(screen.getByTestId("send").hasAttribute("disabled")).toBe(false);
    fireEvent.click(screen.getByTestId("send"));
    expect(onSend).toHaveBeenCalled();
  });
});

describe("SendBox — attaching files", () => {
  const attachProps = {
    draft: draft(),
    connected: true,
    isStreaming: false,
    streamingMessageId: null,
    onUpdate: vi.fn(),
    onSend: vi.fn(),
    onStop: vi.fn(),
  };

  it("hides attaching entirely when the host provides no handler", () => {
    // The kit must stay usable by a host with no upload endpoint.
    render(<SendBox {...attachProps} />);
    expect(screen.queryByRole("button", { name: /attach/i })).toBeNull();
  });

  it("hands picked files to the host", () => {
    const onAttach = vi.fn();
    render(<SendBox {...attachProps} onAttach={onAttach} />);

    const input = screen.getByTestId("attachment-input") as HTMLInputElement;
    const file = new File(["x"], "shot.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [file] } });

    expect(onAttach).toHaveBeenCalledWith([file]);
  });

  it("takes a pasted screenshot", () => {
    // The point on desktop: a screenshot is already on the clipboard, and
    // making people save it to disk first is most of the friction.
    const onAttach = vi.fn();
    render(<SendBox {...attachProps} onAttach={onAttach} />);

    const file = new File(["x"], "clip.png", { type: "image/png" });
    fireEvent.paste(screen.getByRole("textbox"), { clipboardData: { files: [file] } });

    expect(onAttach).toHaveBeenCalledWith([file]);
  });

  it("renders a chip per staged file, and marks one still uploading", () => {
    render(
      <SendBox
        {...attachProps}
        onAttach={vi.fn()}
        attachments={[
          { id: "a1", filename: "done.png" },
          { id: "a2", filename: "slow.png", uploading: true },
        ]}
      />,
    );

    expect(screen.getByText("done.png")).toBeTruthy();
    expect(screen.getByText("slow.png")).toBeTruthy();
    expect(screen.getByText(/uploading/)).toBeTruthy();
  });

  it("cannot remove a chip that is still uploading", () => {
    // There is no server-side id to remove yet.
    render(
      <SendBox
        {...attachProps}
        onAttach={vi.fn()}
        onRemoveAttachment={vi.fn()}
        attachments={[{ id: "a2", filename: "slow.png", uploading: true }]}
      />,
    );
    expect(screen.queryByRole("button", { name: /remove slow.png/i })).toBeNull();
  });

  it("removes a staged file on request", () => {
    const onRemoveAttachment = vi.fn();
    render(
      <SendBox
        {...attachProps}
        onAttach={vi.fn()}
        onRemoveAttachment={onRemoveAttachment}
        attachments={[{ id: "a1", filename: "done.png" }]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /remove done.png/i }));
    expect(onRemoveAttachment).toHaveBeenCalledWith("a1");
  });

  it("shows why an upload failed instead of dropping it silently", () => {
    render(
      <SendBox
        {...attachProps}
        onAttach={vi.fn()}
        attachments={[{ id: "a3", filename: "huge.png", error: "file is larger than the 10MB limit" }]}
      />,
    );
    expect(screen.getByText(/10MB limit/)).toBeTruthy();
  });

  it("blocks send while an attachment is still uploading", () => {
    // A send issued in this window has nothing server-side to claim yet
    // (services.claim_pending_attachments runs synchronously at send time) —
    // the file lands moments later as an orphaned row, unattached to anything.
    const onSend = vi.fn();
    render(
      <SendBox
        {...attachProps}
        onSend={onSend}
        onAttach={vi.fn()}
        attachments={[{ id: "a2", filename: "slow.png", uploading: true }]}
      />,
    );
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "look at this" } });

    expect(screen.getByRole("button", { name: /send/i }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: /send/i }));
    expect(onSend).not.toHaveBeenCalled();
  });

  it("blocks Enter too while an attachment is still uploading", () => {
    const onSend = vi.fn();
    render(
      <SendBox
        {...attachProps}
        onSend={onSend}
        onAttach={vi.fn()}
        attachments={[{ id: "a2", filename: "slow.png", uploading: true }]}
      />,
    );
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "look at this" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(onSend).not.toHaveBeenCalled();
  });

  it("allows send again once every attachment has finished uploading", () => {
    const onSend = vi.fn();
    render(
      <SendBox
        {...attachProps}
        onSend={onSend}
        onAttach={vi.fn()}
        attachments={[{ id: "a2", filename: "slow.png" }]}
      />,
    );
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "look at this" } });

    fireEvent.click(screen.getByRole("button", { name: /send/i }));
    expect(onSend).toHaveBeenCalled();
  });

  it("a failed upload does not block sending the rest of the message", () => {
    // Unlike `uploading`, `error` means the upload is DONE (unsuccessfully) —
    // nothing is pending, so there is nothing a send would race against.
    const onSend = vi.fn();
    render(
      <SendBox
        {...attachProps}
        onSend={onSend}
        onAttach={vi.fn()}
        attachments={[{ id: "a3", filename: "huge.png", error: "file is larger than the 10MB limit" }]}
      />,
    );
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "never mind the file" } });

    fireEvent.click(screen.getByRole("button", { name: /send/i }));
    expect(onSend).toHaveBeenCalled();
  });

  it("does not offer attaching while sending is blocked", () => {
    // Attaching used to be gated on a teammate's draft lock, which no longer
    // exists; the one real reason to withhold it now is `disabledReason`.
    render(
      <SendBox
        {...attachProps}
        onAttach={vi.fn()}
        disabledReason="answer the question above to continue"
      />,
    );
    expect(screen.queryByRole("button", { name: /attach/i })).toBeNull();
  });
});

/**
 * A half-typed message must survive leaving the page.
 *
 * It lives nowhere but this component's state: single-player never mirrors the
 * body to the server (drafts.shouldSyncDraftLive), and the adopt rule above
 * deliberately ignores our OWN server draft — so an unmount used to destroy the
 * only copy. `persistKey` gives it somewhere to land.
 */
describe("SendBox — draft persistence across unmount", () => {
  function fakeStorage(seed: Record<string, string> = {}) {
    const map = new Map(Object.entries(seed));
    return {
      map,
      getItem: (k: string) => map.get(k) ?? null,
      setItem: (k: string, v: string) => void map.set(k, v),
      removeItem: (k: string) => void map.delete(k),
    };
  }

  function mount(
    storage: ReturnType<typeof fakeStorage>,
    props: Partial<Parameters<typeof SendBox>[0]> = {},
  ) {
    return render(
      <SendBox
        draft={draft()}
        connected
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
        persistKey="sess-1"
        storage={storage}
        {...props}
      />,
    );
  }

  const box = () => screen.getByRole("textbox") as HTMLTextAreaElement;

  it("restores what was typed after unmounting and mounting again", () => {
    const storage = fakeStorage();
    const first = mount(storage);
    fireEvent.change(box(), { target: { value: "the thing I was saying" } });
    first.unmount();

    mount(storage);
    expect(box().value).toBe("the thing I was saying");
  });

  it("comes back empty once the message has been sent", () => {
    const storage = fakeStorage();
    const onSend = vi.fn();
    const first = mount(storage, { onSend });
    fireEvent.change(box(), { target: { value: "shipping it" } });
    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
    expect(onSend).toHaveBeenCalled();
    first.unmount();

    mount(storage);
    expect(box().value).toBe("");
  });

  it("does not carry one session's text into another", () => {
    const storage = fakeStorage();
    const first = mount(storage, { persistKey: "sess-1" });
    fireEvent.change(box(), { target: { value: "for session one" } });
    first.unmount();

    mount(storage, { persistKey: "sess-2" });
    expect(box().value).toBe("");
  });

  it("follows the key when the panel swaps sessions without remounting", () => {
    const storage = fakeStorage();
    const view = mount(storage, { persistKey: "sess-1" });
    fireEvent.change(box(), { target: { value: "for session one" } });

    view.rerender(
      <SendBox
        draft={draft()}
        connected
        isStreaming={false}
        streamingMessageId={null}
        onUpdate={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
        persistKey="sess-2"
        storage={storage}
      />,
    );
    expect(box().value).toBe("");

    // ...and session one is still waiting where we left it.
    view.unmount();
    mount(storage, { persistKey: "sess-1" });
    expect(box().value).toBe("for session one");
  });

  it("prefers the stored body over the server draft", () => {
    // The stored one is strictly newer: it is what was in the box when we left.
    const storage = fakeStorage();
    const first = mount(storage, { draft: draft({ body: "from the server" }) });
    fireEvent.change(box(), { target: { value: "what I actually typed" } });
    first.unmount();

    mount(storage, { draft: draft({ body: "from the server" }) });
    expect(box().value).toBe("what I actually typed");
  });

  it("still shows the server draft when nothing was stored", () => {
    mount(fakeStorage(), { draft: draft({ body: "from the server" }) });
    expect(box().value).toBe("from the server");
  });

  it("without a persistKey, behaves exactly as it did before", () => {
    const storage = fakeStorage();
    const first = mount(storage, { persistKey: undefined });
    fireEvent.change(box(), { target: { value: "nowhere to land" } });
    first.unmount();
    expect(storage.map.size).toBe(0);

    mount(storage, { persistKey: undefined });
    expect(box().value).toBe("");
  });

  it("keeps typing working when storage throws", () => {
    const hostile = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("SecurityError");
      },
      removeItem: () => {
        throw new Error("SecurityError");
      },
    };
    mount(hostile as unknown as ReturnType<typeof fakeStorage>);
    fireEvent.change(box(), { target: { value: "still typeable" } });
    expect(box().value).toBe("still typeable");
  });
});

describe("SendBox — typing visibility control", () => {
  it("is absent when neither prop is given", () => {
    setup();
    expect(screen.queryByTestId("typing-visibility")).toBeNull();
  });

  it("is absent when only the value is given, with no handler", () => {
    setup({ typingVisibility: "live" });
    expect(screen.queryByTestId("typing-visibility")).toBeNull();
  });

  it("shows the control when both props are given, and calls the handler on selection", () => {
    const onChange = vi.fn();
    setup({ typingVisibility: "live", onTypingVisibilityChange: onChange });
    const control = screen.getByTestId("typing-visibility") as HTMLSelectElement;
    expect(control.value).toBe("live");
    fireEvent.change(control, { target: { value: "typing" } });
    expect(onChange).toHaveBeenCalledWith("typing");
  });

  it("has a tooltip explaining what it does", () => {
    setup({ typingVisibility: "hidden", onTypingVisibilityChange: vi.fn() });
    const control = screen.getByTestId("typing-visibility") as HTMLSelectElement;
    expect(control.title.length).toBeGreaterThan(0);
  });
});
