import { useState } from "react";

/** A candidate runner the user may re-place a queued turn onto. */
export interface PlacementRunner {
  id: string;
  name: string;
  online: boolean;
}

export interface PlacementBannerProps {
  /** The bound-but-offline runner's display name. */
  runnerName: string;
  /** Alternatives the user may re-place onto (the "Continue on…" picker). */
  eligibleRunners: PlacementRunner[];
  /** True while a placement POST is in flight — disables both actions. */
  busy?: boolean;
  /** A failure message to surface below the actions (e.g. "no pending
   *  message to place"). Rendered with destructive styling. */
  error?: string | null;
  /** A non-failure status message (e.g. "Placed — the new runner will pick it
   *  up shortly."). Rendered muted, visually distinct from `error`. */
  info?: string | null;
  /** Keep the turn queued for the bound runner to come back online. */
  onWait: () => void;
  /** Re-place the turn onto the given runner id. */
  onPlace: (runnerId: string) => void;
  /** True when the runner is PARKED rather than gone — a decision someone made,
   *  which someone can therefore undo. Changes the headline and, with `onResume`,
   *  puts the one-tap fix first. */
  paused?: boolean;
  /** The reason recorded at pause time, shown so the reader can judge whether
   *  resuming is safe (e.g. "token limit on this account" usually is not). */
  pausedNote?: string;
  /** Un-park the runner in place. Omit when the viewer cannot — only the human
   *  who paired a runner may pause or resume it, so offering this to anyone else
   *  would render a button that 404s. */
  onResume?: () => void;
}

/**
 * Offline-runner placement banner: the chat session's bound runner cannot act,
 * and the user must get it acting again — resume it, or continue on a different
 * session-capable runner. Presentational only — NO fetch, NO WS. The container
 * (canopy's ChatPage) owns the fleet poll, the derived eligible-runner list, and
 * the placement POST; this component just renders the decision and forwards it.
 *
 * The actions are deliberately not peers. Waiting means your message sits QUEUED
 * until the box returns — which, for a pause you applied yourself, may be never —
 * and a queued send reads as a sent one right up until you notice no reply came.
 * So the exits that make the chat WORK are buttons, and waiting is a link you
 * have to mean. It stays reachable because a box rebooting in ninety seconds is
 * a real case; it is just not the shape of the default.
 */
export function PlacementBanner({
  runnerName,
  eligibleRunners,
  busy = false,
  error,
  info,
  onWait,
  onPlace,
  paused = false,
  pausedNote,
  onResume,
}: PlacementBannerProps) {
  // Whether the "Continue on…" picker is expanded — purely local UI state,
  // not fetch-driven, so it lives in the kit rather than round-tripping
  // through the container.
  const [showPicker, setShowPicker] = useState(false);

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-warning/30 bg-warning/10 px-4 py-2 text-[12px] text-warning">
      <span className="font-medium">
        {runnerName} is {paused ? "paused" : "unavailable"}
      </span>
      {paused && pausedNote && (
        <span className="text-warning/80">— {pausedNote}</span>
      )}
      {onResume && (
        <button
          type="button"
          onClick={onResume}
          disabled={busy}
          className="rounded-md border border-warning/40 bg-warning/20 px-2 py-0.5 font-medium text-warning hover:bg-warning/30 disabled:opacity-50"
        >
          Resume
        </button>
      )}
      <button
        type="button"
        onClick={() => setShowPicker((v) => !v)}
        disabled={busy}
        className="rounded-md border border-warning/40 px-2 py-0.5 text-warning hover:bg-warning/20 disabled:opacity-50"
      >
        Continue on…
      </button>
      {showPicker && (
        <select
          defaultValue=""
          disabled={busy}
          onChange={(e) => onPlace(e.target.value)}
          className="rounded-md border border-warning/40 bg-card px-1.5 py-0.5 text-[12px] text-foreground disabled:opacity-50"
          aria-label="Continue on"
        >
          <option value="" disabled>
            Choose a runner…
          </option>
          {eligibleRunners.map((r) => (
            <option key={r.id} value={r.id}>
              {r.online ? "●" : "○"} {r.name}
            </option>
          ))}
        </select>
      )}
      {/* Demoted, not removed — see the component doc. Still a <button> so it
          keeps its role, its disabled state and its accessible name. */}
      <button
        type="button"
        onClick={onWait}
        disabled={busy}
        className="ml-auto text-warning/70 underline underline-offset-2 hover:text-warning disabled:opacity-50"
      >
        Wait for it
      </button>
      {error && <span className="text-destructive">{error}</span>}
      {info && <span className="text-muted-foreground">{info}</span>}
    </div>
  );
}
