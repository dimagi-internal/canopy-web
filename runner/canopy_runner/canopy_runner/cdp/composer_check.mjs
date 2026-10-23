// Is the composer holding a HUMAN's text in front of the message we just typed?
//
// open-send reads the composer before it types and hands a non-empty one back as a
// `collision`, so the human is asked before anything of theirs is touched. On
// 2026-10-23 (turn 64cada76, failure investigation 2) that read came back EMPTY while
// Jonathan's half-typed "okay great, let" sat in ACE's composer — the same read
// finds it today, so the miss was the frame, not the rule. The chat message was
// typed onto the end of his draft; the post-Enter recheck saw "not ours", stopped
// quietly, and the sidecar reported `sent`. Nothing landed and no dialog came up.
//
// So the sidecar now looks AFTER typing and BEFORE Enter as well. insertText puts
// our message at the cursor, which is the end of whatever is already there, so a
// human's draft shows up as a PREFIX. These rules decide that from the rendered
// line. Pure (no playwright) so they run under plain node in tests.
import { asTyped } from './typed_text.mjs';

// Whitespace and box-drawing edges out: the screen wraps, indents and frames what
// was typed, and none of that is part of the words.
export const squash = (s) => (s || '').replace(/[\s│|]+/g, '');

// How much of the line's start must match our message's start to call it ours.
// Short enough that a wrapped or partly collapsed line still matches, long enough
// that a human's draft opening with the same word does not.
export const PROBE_CHARS = 12;

const PASTED_AT_START = /^\[Pasted text #\d+/;

// True when `typed` (the composer's human-typed text, dim suggestions excluded)
// holds something other than our message at its START. Empty is not foreign.
// A collapsed paste placeholder first is ours: the composer was read empty before
// we typed, so a placeholder leading the line can only be our own paste.
export function hasForeignLead(body, typed) {
  const t = (typed || '').trim();
  if (!t) return false;
  if (PASTED_AT_START.test(t)) return false;
  const line = squash(t);
  const ours = squash(asTyped(body));
  if (!ours) return true;
  const n = Math.min(PROBE_CHARS, line.length, ours.length);
  const head = line.slice(0, n);
  // Starting anywhere INSIDE our message is still ours: a composer too tall for the
  // pane shows only its tail, and a draft rarely opens with words from our middle.
  return head !== ours.slice(0, n) && !ours.includes(head);
}

// What the line should read once our message is taken back off its END, squashed,
// or null when our message is not at the end as typed (a collapsed paste, or text
// typed after ours) — then nothing is deleted, because a Backspace could only be
// aimed at the human's words.
export function draftWithoutOurs(body, typed) {
  const line = squash(typed);
  const ours = squash(asTyped(body));
  if (!ours || line.length <= ours.length || !line.endsWith(ours)) return null;
  return line.slice(0, line.length - ours.length);
}
