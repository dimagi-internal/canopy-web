// The sidecar's arguments: one JSON object, read from STDIN.
//
// They used to be argv[3]. On Windows a command line is capped at 32,767
// characters, so a long prompt failed to even start node (WinError 206) and the
// turn died on an uncaught OSError. stdin has no such cap. argv[3] is still read
// when present, so the sidecar can be driven by hand: `node emdash_control.mjs
// list '{"port":9222}'` (or pipe the JSON in).
//
// Pure (no playwright) so it runs under plain node in tests.
export async function readArgs(argv = process.argv, stdin = process.stdin) {
  if (argv[3] !== undefined) return JSON.parse(argv[3] || '{}');
  if (stdin.isTTY) return {};
  let raw = '';
  stdin.setEncoding('utf8');
  for await (const chunk of stdin) raw += chunk;
  return JSON.parse(raw.trim() || '{}');
}
