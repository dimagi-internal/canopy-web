/**
 * Dictation is the composer's answer to "let me talk instead of type".
 *
 * A web page cannot put the Android keyboard into its mic mode — no such API
 * exists — so the mic button drives SpeechRecognition directly and writes into
 * the same local-first draft the keyboard writes into. Two properties matter
 * and are pinned here:
 *
 *   * FINAL text goes into the draft; INTERIM text never does. The draft syncs
 *     to the server on every change, and streaming provisional recognition
 *     through it would churn the draft version on words about to be rewritten.
 *   * Where the API is absent (most desktop Firefox, older iOS) the hook says
 *     unsupported and the button is not rendered at all — no dead control.
 */
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, fireEvent } from "@testing-library/react";

import { SendBox } from "./SendBox";
import type { Draft } from "./protocol";

const ME = 1;

interface FakeResult {
  transcript: string;
  isFinal: boolean;
}

/** Stands in for the browser's SpeechRecognition, which jsdom has none of. */
class FakeRecognition {
  static instances: FakeRecognition[] = [];
  continuous = false;
  interimResults = false;
  lang = "";
  onresult: ((event: unknown) => void) | null = null;
  onend: (() => void) | null = null;
  started = false;
  aborted = false;

  constructor() {
    FakeRecognition.instances.push(this);
  }

  start() {
    if (this.started) throw new Error("InvalidStateError");
    this.started = true;
  }

  stop() {
    // The real API funnels every stop through onend, including this one.
    this.onend?.();
  }

  abort() {
    this.aborted = true;
  }

  emit(results: FakeResult[], resultIndex = 0) {
    const list = results.map((r) => ({
      isFinal: r.isFinal,
      0: { transcript: r.transcript },
      length: 1,
    }));
    act(() => {
      this.onresult?.({ resultIndex, results: list });
    });
  }

  static latest(): FakeRecognition {
    const rec = FakeRecognition.instances.at(-1);
    if (!rec) throw new Error("no recognition was constructed");
    return rec;
  }
}

function installSpeechRecognition() {
  FakeRecognition.instances = [];
  (window as unknown as Record<string, unknown>).SpeechRecognition =
    FakeRecognition;
}

afterEach(() => {
  cleanup();
  delete (window as unknown as Record<string, unknown>).SpeechRecognition;
  delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
  FakeRecognition.instances = [];
});

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
  render(
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
  const mic = () => screen.queryByRole("button", { name: /dictat/i });
  return { textarea, mic, onUpdate, onSend };
}

describe("SendBox — dictation", () => {
  it("renders no mic where the browser has no SpeechRecognition", () => {
    const { mic } = setup();
    expect(mic()).toBeNull();
  });

  it("finds the webkit-prefixed constructor (iOS Safari)", () => {
    FakeRecognition.instances = [];
    (window as unknown as Record<string, unknown>).webkitSpeechRecognition =
      FakeRecognition;
    const { mic } = setup();
    expect(mic()).not.toBeNull();
  });

  it("starts recognition on click and stops it on a second click", () => {
    installSpeechRecognition();
    const { mic } = setup();

    fireEvent.click(mic()!);
    const rec = FakeRecognition.latest();
    expect(rec.started).toBe(true);
    expect(rec.continuous).toBe(true);
    expect(rec.interimResults).toBe(true);
    // The label flips so the control says what the next tap does.
    expect(screen.getByRole("button", { name: /stop dictation/i })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /stop dictation/i }));
    expect(screen.getByRole("button", { name: /start dictation/i })).toBeTruthy();
  });

  it("writes a finalized utterance into the draft and syncs it", () => {
    installSpeechRecognition();
    const { mic, textarea, onUpdate } = setup();

    fireEvent.click(mic()!);
    FakeRecognition.latest().emit([
      { transcript: "ship the runner fix", isFinal: true },
    ]);

    expect(textarea().value).toBe("ship the runner fix");
    expect(onUpdate).toHaveBeenLastCalledWith("ship the runner fix");
  });

  it("shows interim text without putting it in the draft", () => {
    installSpeechRecognition();
    const { mic, textarea, onUpdate } = setup();

    fireEvent.click(mic()!);
    FakeRecognition.latest().emit([
      { transcript: "half a thought", isFinal: false },
    ]);

    expect(textarea().value).toBe("");
    expect(onUpdate).not.toHaveBeenCalled();
    expect(screen.getByText(/half a thought/)).toBeTruthy();
  });

  it("appends to what is already typed, with one space between", () => {
    installSpeechRecognition();
    const { mic, textarea } = setup();

    fireEvent.change(textarea(), { target: { value: "hey " } });
    fireEvent.click(mic()!);
    FakeRecognition.latest().emit([{ transcript: "there", isFinal: true }]);

    expect(textarea().value).toBe("hey there");
  });

  it("keeps both utterances when two finals land before a re-render", () => {
    installSpeechRecognition();
    const { mic, textarea } = setup();

    fireEvent.click(mic()!);
    const rec = FakeRecognition.latest();
    // Two separate onresult events in one tick — the second must not read a
    // stale body and clobber the first.
    rec.emit([{ transcript: "one", isFinal: true }]);
    rec.emit([{ transcript: "two", isFinal: true }]);

    expect(textarea().value).toBe("one two");
  });

  it("only reads results from resultIndex forward", () => {
    installSpeechRecognition();
    const { mic, textarea } = setup();

    fireEvent.click(mic()!);
    // The API hands back the whole session's results each time; everything
    // before resultIndex was already consumed.
    FakeRecognition.latest().emit(
      [
        { transcript: "already used", isFinal: true },
        { transcript: "new", isFinal: true },
      ],
      1,
    );

    expect(textarea().value).toBe("new");
  });

  it("stops listening when the message is sent", () => {
    installSpeechRecognition();
    const { mic, textarea, onSend } = setup();

    fireEvent.click(mic()!);
    FakeRecognition.latest().emit([{ transcript: "send it", isFinal: true }]);
    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));

    expect(onSend).toHaveBeenCalled();
    expect(textarea().value).toBe("");
    expect(screen.getByRole("button", { name: /start dictation/i })).toBeTruthy();
  });

  it("hides the mic when the composer is blocked", () => {
    installSpeechRecognition();
    const { mic } = setup({ disabledReason: "Runner offline" });
    expect(mic()).toBeNull();
  });

  it("hides the mic while a teammate holds the draft", () => {
    installSpeechRecognition();
    const { mic } = setup({
      holderIsPresent: true,
      draft: draft({ last_editor: 2, last_edit_at: new Date().toISOString() }),
    });
    expect(mic()).toBeNull();
  });

  it("aborts recognition on unmount so the mic light goes out", () => {
    installSpeechRecognition();
    const { mic } = setup();

    fireEvent.click(mic()!);
    const rec = FakeRecognition.latest();
    cleanup();

    expect(rec.aborted).toBe(true);
  });
});
