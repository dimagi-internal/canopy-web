// The pipe broker's bearer token, when one serves this port (canopy_runner/cdp_pipe.py).
//
// In pipe mode emdash opens no debug port; `canopy-runner emdash-pipe` owns its
// DevTools pipe and serves the runner's usual cdp_port, answering only the token it
// writes to ~/.canopy/cdp-pipe.json (0600, so another macOS account cannot read it).
// No file, or a file for another port, means port mode: no headers, nothing changes.
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

export const STATE_PATH = join(homedir(), '.canopy', 'cdp-pipe.json');

export function pipeHeaders(port, statePath = STATE_PATH) {
  let data;
  try {
    data = JSON.parse(readFileSync(statePath, 'utf8'));
  } catch {
    return {};
  }
  if (Number(data.port) !== Number(port) || !data.token) return {};
  return { Authorization: `Bearer ${data.token}` };
}
