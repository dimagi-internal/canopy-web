/** A reconnect delay spread over ±50%: `ms * (0.5 + random())`. When a server task
 *  stops (a scale-in or a deploy), every socket on it drops at the same instant;
 *  without the spread they all come back on the same tick and land on the tasks
 *  left as one burst of handshakes. */
export function jittered(ms: number, random: () => number = Math.random): number {
  return Math.round(ms * (0.5 + random()))
}
