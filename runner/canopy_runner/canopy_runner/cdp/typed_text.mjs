// What the sidecar actually types for a message, so that it lands as TEXT.
//
// Claude Code reads a single keystroke into an EMPTY prompt as a key binding, not
// a character. Measured on a raw pty against claude v2.1.304 (2026-10-20):
//   "?"  opens the keyboard-shortcuts panel; the prompt stays empty
//   "!"  switches to shell mode with an empty command
//   "/"  opens the slash-command menu — Enter then runs its FIRST command
// insertText of a lone character is exactly that one keystroke, so a chat message
// of just "?" never reached the session, and each recovery retype only toggled the
// panel (turn e4fb721c, failure investigation 2, 2026-10-20).
//
// The same character arriving with a leading space is ordinary text in all three
// cases (same pty, same version), and delivery matching squashes whitespace, so
// " ?" in the transcript still confirms "?". Only a message that IS one of these
// characters is touched: "/ace:turn" and "!ls" are meant as a command and shell
// mode, and stay exactly as sent.
//
// Pure (no playwright) so the rule runs under plain node in tests.
export const KEY_BINDINGS = new Set(['?', '!', '/']);

export function asTyped(body) {
  const text = body || '';
  return KEY_BINDINGS.has(text.trim()) ? ' ' + text.trim() : text;
}
