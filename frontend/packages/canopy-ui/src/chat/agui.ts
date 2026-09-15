/**
 * AG-UI events, read back into canopy's own session frames.
 *
 * The inverse of `apps/canopy_sessions/agui.py`. The server projects canopy →
 * AG-UI; this reads AG-UI → canopy, so `sessionReducer` never learns a second
 * vocabulary and every existing surface keeps working unchanged.
 *
 * **Why an inverse rather than a second reducer.** Teaching the reducer AG-UI
 * directly would mean two code paths producing the same `SessionState`, and
 * they would diverge — the multiplayer cases especially, which AG-UI does not
 * model and which only canopy's path exercises. One reducer with a translator
 * in front of it has a single behaviour to test.
 *
 * **The round trip is proved, not assumed.** `agui.fixture.json` is generated
 * by the Python projection; a Python test asserts it is current and the test
 * beside this one asserts this module recovers the original canopy frame from
 * it. The projection and its inverse live in different languages, so a shared
 * artifact is the only thing that can catch them drifting apart.
 *
 * **Lossless via `metadata`.** AG-UI has no slot for canopy's transcript
 * ordinal (`turn_index`) or for the settled text of a whole message
 * (`plaintext`), and the reducer needs both — one to sort a live row into the
 * position it will occupy after a reload, the other to tell a web send from the
 * echo of the same text arriving from the runner. They ride `metadata.canopy`,
 * which is the extension point AG-UI reserves ("Every other key is user
 * space"). Without them the projection would be a downgrade dressed as a
 * standard.
 */

import type { SessionMenu, WsEvent } from "./protocol";

/** Where canopy's own fields ride. Mirrors `agui.METADATA_KEY`. */
export const METADATA_KEY = "canopy";

/** Prefix on canopy's `CUSTOM` events. Mirrors `agui.CUSTOM_PREFIX`. */
export const CUSTOM_PREFIX = "canopy.";

/** An AG-UI event as it arrives on the wire: camelCase, unknown shape. */
type AguiFrame = Record<string, unknown>;

function meta(frame: AguiFrame): Record<string, unknown> {
  const m = frame.metadata as Record<string, unknown> | undefined;
  const mine = m?.[METADATA_KEY] as Record<string, unknown> | undefined;
  return mine ?? {};
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/**
 * One AG-UI event → zero or more canopy frames.
 *
 * Zero is a real answer, twice over. Some AG-UI events have no canopy meaning
 * (`RUN_STARTED` duplicates an activity frame canopy already sent); and an
 * unknown event returns zero rather than throwing, because a protocol that
 * gains an event type must not be able to break a client that has not been
 * updated yet — which, for a 0.x protocol, is a matter of when rather than if.
 */
export function fromAgui(frame: AguiFrame): WsEvent[] {
  const type = typeof frame.type === "string" ? frame.type : "";
  const m = meta(frame);

  switch (type) {
    case "TEXT_MESSAGE_START":
      return [
        {
          event: "chat.stream_start",
          data: { message_id: str(frame.messageId), turn_index: num(m.turn_index) },
        },
      ];

    case "TEXT_MESSAGE_CONTENT":
      return [
        {
          event: "chat.delta",
          data: { message_id: str(frame.messageId), text: str(frame.delta) },
        },
      ];

    case "TEXT_MESSAGE_END":
      return [
        {
          event: "chat.stream_complete",
          data: { message_id: str(frame.messageId), plaintext: str(m.plaintext) },
        },
      ];

    case "TEXT_MESSAGE_CHUNK":
      // Only the USER variant is canopy's `chat.user_message` — a human typing
      // into emdash rather than into this page. An assistant chunk would be a
      // whole reply arriving at once, which canopy's runner does not produce;
      // mapping it to `stream_complete` with no preceding `stream_start` would
      // hand the reducer a completion for a message it never opened.
      if (str(frame.role) !== "user") return [];
      return [
        {
          event: "chat.user_message",
          data: {
            message_id: str(frame.messageId),
            turn_index: num(m.turn_index),
            plaintext: str(frame.delta),
          },
        },
      ];

    case "TOOL_CALL_START":
      // canopy carries the tool's payload on one frame; AG-UI streams the
      // arguments on the ARGS event that follows. So nothing is emitted until
      // ARGS arrives — a row rendered on START would have no input, which is
      // the half-empty-tool-call bug the ACP notes warn about.
      pendingCalls.set(str(frame.toolCallId), {
        name: str(frame.toolCallName),
        parentMessageId: str(frame.parentMessageId),
        turnIndex: num(m.turn_index),
        block: m.block as Record<string, unknown> | undefined,
      });
      return [];

    case "TOOL_CALL_ARGS": {
      const id = str(frame.toolCallId);
      const pending = pendingCalls.get(id);
      if (!pending) return [];
      let input: unknown = {};
      try {
        input = JSON.parse(str(frame.delta) || "{}");
      } catch {
        // A fragment that is not valid JSON on its own is legal in AG-UI. The
        // server sends arguments whole, so this means genuinely malformed
        // input, and an empty object is a better row than a dropped one.
        input = {};
      }
      // The runner's own payload where we have it, verbatim. canopy passes the
      // block straight through from the transcript and never interprets it, so
      // its shape belongs to the PRODUCER — reconstructing one from the AG-UI
      // fields would invent a `type` and an `id` the real payload may not have
      // carried, and hand the renderer something no runner ever sent.
      const block = pending.block ?? { name: pending.name, input };
      return [
        {
          event: "chat.tool_use",
          data: {
            parent_message_id: pending.parentMessageId || null,
            tool_message_id: id,
            turn_index: pending.turnIndex,
            block,
          },
        },
      ];
    }

    case "TOOL_CALL_END":
      pendingCalls.delete(str(frame.toolCallId));
      return [];

    case "TOOL_CALL_RESULT":
      return [
        {
          event: "chat.tool_result",
          data: {
            parent_message_id: (m.parent_message_id as string | undefined) ?? null,
            tool_message_id: str(frame.toolCallId),
            turn_index: num(m.turn_index),
            block: (m.block as Record<string, unknown> | undefined) ?? { content: frame.content },
          },
        },
      ];

    case "ACTIVITY_SNAPSHOT": {
      const content = (frame.content as Record<string, unknown>) ?? {};
      const state = str(content.state);
      if (!state) return [];
      return [{ event: "session.activity", data: { state: state as "working" | "idle" | "blocked" } }];
    }

    case "RUN_FINISHED": {
      const outcome = frame.outcome as Record<string, unknown> | undefined;
      if (outcome?.type === "interrupt") {
        const interrupts = (outcome.interrupts as Array<Record<string, unknown>>) ?? [];
        return interrupts.map((i) => ({
          event: "session.menu" as const,
          data: { menu: interruptToMenu(i) },
        }));
      }
      const result = frame.result as Record<string, unknown> | undefined;
      if (result?.cancelled) {
        return [
          {
            event: "chat.stream_cancelled",
            data: { message_id: null, partial_len: num(result.partial_len) },
          },
        ];
      }
      return [];
    }

    case "RUN_ERROR":
      return [
        {
          event: "session.error",
          data: { code: str(frame.code) || "run_error", message: str(frame.message) },
        },
      ];

    case "STATE_DELTA": {
      // Only the one patch canopy sends. A general JSON-Patch applier would be
      // a second source of truth for session state, which is the reducer's job.
      const ops = (frame.delta as Array<Record<string, unknown>>) ?? [];
      const title = ops.find((op) => op.path === "/title");
      if (!title) return [];
      return [{ event: "session.title_updated", data: { title: str(title.value) } }];
    }

    case "CUSTOM": {
      // canopy's own vocabulary, coming home. Drafts, presence and placement
      // have no AG-UI spelling because the protocol models one user and one
      // agent; they were namespaced on the way out and are unwrapped here.
      const name = str(frame.name);
      if (!name.startsWith(CUSTOM_PREFIX)) return [];
      const event = name.slice(CUSTOM_PREFIX.length);
      if (event === "menu.retracted") return [{ event: "session.menu", data: { menu: null } }];
      if (event === "menu") {
        return [{ event: "session.menu", data: { menu: frame.value as never } }];
      }
      return [{ event, data: frame.value } as unknown as WsEvent];
    }

    default:
      return [];
  }
}

/** Arguments arrive on a later event than the tool's name, so the head of a
 *  call is held until they do. Module-level because a socket is a stream and
 *  the pairing spans events; `resetAguiState` exists so a test — and a
 *  reconnect — can start from nothing rather than inheriting a half-read call
 *  from the connection before. */
const pendingCalls = new Map<
  string,
  {
    name: string;
    parentMessageId: string;
    turnIndex: number;
    block?: Record<string, unknown>;
  }
>();

export function resetAguiState(): void {
  pendingCalls.clear();
}

/** An AG-UI interrupt back into canopy's menu shape.
 *
 *  The fields line up because the protocol independently arrived at the same
 *  ones: `expiresAt` is canopy's `observed_at` (a dialog lives on a terminal
 *  and this is a copy, so it has to carry its own age), and the option list
 *  lives in `metadata.canopy.questions` because a response schema can say a
 *  field takes a list but not that the TUI draws it as checkboxes a number key
 *  TOGGLES rather than answers. */
function interruptToMenu(interrupt: Record<string, unknown>): SessionMenu {
  const m = (interrupt.metadata as Record<string, unknown> | undefined)?.[METADATA_KEY] as
    | Record<string, unknown>
    | undefined;
  const questions = (m?.questions as Array<Record<string, unknown>>) ?? [];
  const first = questions[0] ?? {};
  return {
    question: str(first.question) || str(interrupt.message),
    title: str(interrupt.message),
    body: str(m?.body),
    source: str(interrupt.reason),
    questions: questions as unknown as SessionMenu["questions"],
    options: (first.options as unknown as SessionMenu["options"]) ?? [],
    answer_error: str(m?.answer_error) || undefined,
    answer_note: str(m?.answer_note) || undefined,
    restored: Boolean(m?.restored),
    observed_at: interrupt.expiresAt ? Date.parse(str(interrupt.expiresAt)) / 1000 : undefined,
  };
}
