import type { ContextUsage, Message } from "./protocol";

/**
 * How full the session's context window is — the number Claude Code shows in
 * its own footer, read from the transcript.
 *
 * Pure, no React, so the arithmetic and the "say unknown rather than guess"
 * rules are tested without mounting anything.
 *
 * The fill is the LATEST model call's prompt (input plus both cache counts),
 * which the runner stamps on the last row of each assistant record as
 * `content.usage` (`canopy_transcript.record_window`). The window's SIZE is the
 * hard part: the transcript records the bare API model id (`claude-opus-5-5`)
 * and never the `[1m]` that picks the 1M window, so it comes from the box's own
 * model setting (`runnerModel`, e.g. `opus[1m]`) — or from the fill itself,
 * since a prompt past 200k tokens can only fit the 1M window.
 */

export const DEFAULT_WINDOW = 200_000;
export const LONG_WINDOW = 1_000_000;

export interface ContextReading {
  /** Tokens in the latest call's prompt; null when no call has said. */
  tokens: number | null;
  /** The window those tokens fill; null when nothing says which it is. */
  window: number | null;
  /** 0–100, rounded; null unless both are known. */
  percent: number | null;
  /** The transcript's model id for the latest call ("" when unknown). */
  model: string;
  /** The conversation compacted after the latest reading: the old number is
   *  gone and the next call will say the new one. */
  compactedSince: boolean;
}

function usageOf(m: Message): ContextUsage | null {
  const u = m.content?.usage as Partial<ContextUsage> | undefined;
  if (!u || typeof u.context_tokens !== "number" || !(u.context_tokens > 0)) return null;
  return { context_tokens: u.context_tokens, model: typeof u.model === "string" ? u.model : "" };
}

/** A `user` row Claude Code wrote when it compacted (`content.record.compact_summary`). */
export function isCompactionSummary(m: Message): boolean {
  const record = m.content?.record as Record<string, unknown> | undefined;
  return m.role === "user" && Boolean(record?.compact_summary);
}

/** The window a call ran in, or null when nothing says. */
export function contextWindow(tokens: number | null, model: string, runnerModel?: string | null): number | null {
  if (/\[1m\]/i.test(model) || /\[1m\]/i.test(runnerModel ?? "")) return LONG_WINDOW;
  if (tokens != null && tokens > DEFAULT_WINDOW) return LONG_WINDOW;
  // The box names a model without `[1m]`: Claude Code's default window.
  if ((runnerModel ?? "").trim()) return DEFAULT_WINDOW;
  return null;
}

/** The reading from the newest rows, or null when the conversation has no
 *  reply yet (nothing to read, and nothing worth saying). */
export function readContext(messages: readonly Message[], runnerModel?: string | null): ContextReading | null {
  let compactedSince = false;
  let replied = false;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i];
    if (m.role !== "user") replied = true;
    const usage = usageOf(m);
    if (usage) {
      const window = contextWindow(usage.context_tokens, usage.model, runnerModel);
      return {
        tokens: usage.context_tokens,
        window,
        percent: window ? Math.min(100, Math.round((usage.context_tokens / window) * 100)) : null,
        model: usage.model,
        compactedSince,
      };
    }
    // Anything older describes the context before it was summarised.
    if (isCompactionSummary(m)) {
      compactedSince = true;
      break;
    }
  }
  if (!replied && !compactedSince) return null;
  return { tokens: null, window: contextWindow(null, "", runnerModel), percent: null, model: "", compactedSince };
}

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}k`;
  return String(n);
}

/** The words: "62% context", "124k context" (window unknown), "context unknown". */
export function contextLabel(r: ContextReading): string {
  if (r.percent != null) return `${r.percent}% context`;
  if (r.tokens != null) return `${compact(r.tokens)} context`;
  return r.compactedSince ? "context compacted" : "context unknown";
}

/** The hover text: the exact numbers, and where the window size came from. */
export function contextTitle(r: ContextReading, runnerModel?: string | null): string {
  const parts: string[] = [];
  if (r.tokens != null) {
    parts.push(
      r.window
        ? `${r.tokens.toLocaleString("en-US")} of ${r.window.toLocaleString("en-US")} tokens in context`
        : `${r.tokens.toLocaleString("en-US")} tokens in context — window size unknown`,
    );
  } else if (r.compactedSince) {
    parts.push("Context compacted — the next reply says how full it is");
  } else {
    parts.push("No reply has said how full the context is yet");
  }
  if (r.model) parts.push(`model ${r.model}`);
  if (runnerModel) parts.push(`runner's model setting ${runnerModel}`);
  return parts.join(" · ");
}

/** Bar colour by fill: plenty, getting full, about to compact. */
export function contextTone(percent: number | null): "ok" | "warn" | "full" {
  if (percent == null || percent < 80) return "ok";
  return percent < 95 ? "warn" : "full";
}
