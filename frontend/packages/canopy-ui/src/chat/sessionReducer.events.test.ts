import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { REDUCER_EVENTS } from "./sessionReducer";

/** The hook passes the reducer only the frames in REDUCER_EVENTS. A case the
 *  list lacks is dead code that looks alive, which is how `draft.committed`,
 *  `presence.*` and `session.turn_status` went unhandled from 2026-09-13 to
 *  2026-09-23 without a single failing test. */
describe("REDUCER_EVENTS", () => {
  it("names exactly the frames the reducer's switch handles", () => {
    const src = readFileSync(fileURLToPath(new URL("./sessionReducer.ts", import.meta.url)), "utf8");
    const cases = new Set([...src.matchAll(/case "([a-z_.]+)":/g)].map((m) => m[1]));
    expect([...cases].sort()).toEqual([...REDUCER_EVENTS].sort());
  });
});
