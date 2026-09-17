/**
 * One REST transcript row -> the kit's `Message`.
 *
 * This lived twice, written out by hand in both hosts that read a transcript
 * over REST and then hand it to this kit: canopy-web's own
 * `pages/chatPageLogic.ts` and ace-web's `canopy/CanopyChatPanel.tsx`, whose
 * copy carried the comment "mirrors canopy-web's own
 * `chatPageLogic.ts::restToKitMessage`" — an accurate description of a bug
 * waiting to happen. It is a pure function OF this kit's own `Message` type
 * against canopy's own `MessageOut` wire schema, so neither host was ever the
 * right owner of it; both were translating between two shapes they had each
 * imported from somewhere else.
 *
 * The input is declared structurally rather than as either host's generated
 * type, because the hosts generate their own: canopy-web has
 * `components["schemas"]["MessageOut"]`, ace-web casts an `unknown` row. Both
 * are assignable to `RestMessage`, and a generated type's `readonly` property
 * modifiers do not affect that.
 */

import type { Message } from "./protocol";

/** canopy's `MessageOut` (apps/canopy_sessions/schemas.py), structurally. */
export interface RestMessage {
  turn_index: number;
  role: string;
  content: Record<string, unknown>;
  plaintext: string;
  created_at: string;
}

/**
 * Synthetic id (`t<turn_index>`) + `status: "complete"`.
 *
 * `prependHistory` dedupes on `turn_index`, not on `id`, so a synthetic id can
 * never collide with the live WS row for the same turn — which is the whole
 * reason it is safe to invent one here. `started_at` is null because a row read
 * back from REST has no streaming history to report; `completed_at` takes
 * `created_at`, which is when the turn finished as far as the server records it.
 */
export function restToKitMessage(m: RestMessage): Message {
  return {
    id: `t${m.turn_index}`,
    turn_index: m.turn_index,
    role: m.role as Message["role"],
    content: m.content,
    plaintext: m.plaintext,
    status: "complete",
    error_detail: null,
    started_at: null,
    completed_at: m.created_at,
    created_at: m.created_at,
  };
}
