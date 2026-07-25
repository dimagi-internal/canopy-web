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
  /** A message to surface below the actions (failure OR informational, e.g.
   *  "no pending message to place" — the kit has a single message slot; the
   *  container decides what belongs in it). */
  error?: string | null;
  /** Keep the turn queued for the bound runner to come back online. */
  onWait: () => void;
  /** Re-place the turn onto the given runner id. */
  onPlace: (runnerId: string) => void;
}

/**
 * Offline-runner placement banner: the chat session's bound runner has gone
 * unavailable, and the user must decide whether to wait for it or continue
 * on a different session-capable runner. Presentational only — NO fetch, NO
 * WS. The container (canopy's ChatPage) owns the fleet poll, the derived
 * eligible-runner list, and the placement POST; this component just renders
 * the decision and forwards the pick.
 */
export function PlacementBanner({
  runnerName,
  eligibleRunners,
  busy = false,
  error,
  onWait,
  onPlace,
}: PlacementBannerProps) {
  // Whether the "Continue on…" picker is expanded — purely local UI state,
  // not fetch-driven, so it lives in the kit rather than round-tripping
  // through the container.
  const [showPicker, setShowPicker] = useState(false);

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-warning/30 bg-warning/10 px-4 py-2 text-[12px] text-warning">
      <span className="font-medium">{runnerName} is unavailable</span>
      <button
        type="button"
        onClick={onWait}
        disabled={busy}
        className="rounded-md border border-warning/40 px-2 py-0.5 text-warning hover:bg-warning/20 disabled:opacity-50"
      >
        Wait for it
      </button>
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
      {error && <span className="text-destructive">{error}</span>}
    </div>
  );
}
