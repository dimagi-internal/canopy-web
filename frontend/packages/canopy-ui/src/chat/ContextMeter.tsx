import { useMemo } from "react";

import type { Message } from "./protocol";
import { contextLabel, contextTitle, contextTone, readContext } from "./contextUsage";

const FILL: Record<ReturnType<typeof contextTone>, string> = {
  ok: "bg-primary",
  warn: "bg-warning",
  full: "bg-destructive",
};

/**
 * "62% context": how full the session's context window is, from the newest
 * reply that said (see `contextUsage.ts`). Renders nothing until the
 * conversation has a reply; says "unknown" rather than guess a window size.
 */
export function ContextMeter({
  messages,
  runnerModel,
}: {
  messages: readonly Message[];
  /** The bound runner's model setting (`opus[1m]`) — how the window is sized. */
  runnerModel?: string | null;
}) {
  const reading = useMemo(() => readContext(messages, runnerModel), [messages, runnerModel]);
  if (!reading) return null;
  const tone = contextTone(reading.percent);
  return (
    <span
      className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground tabular-nums"
      title={contextTitle(reading, runnerModel)}
      data-testid="context-meter"
    >
      {reading.percent != null && (
        <span
          className="relative h-1.5 w-10 overflow-hidden rounded-full bg-muted"
          role="meter"
          aria-label="Context window used"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={reading.percent}
        >
          <span
            className={`absolute inset-y-0 left-0 rounded-full ${FILL[tone]}`}
            style={{ width: `${Math.max(2, reading.percent)}%` }}
          />
        </span>
      )}
      <span className={tone === "full" ? "text-destructive" : tone === "warn" ? "text-warning" : undefined}>
        {contextLabel(reading)}
      </span>
    </span>
  );
}
