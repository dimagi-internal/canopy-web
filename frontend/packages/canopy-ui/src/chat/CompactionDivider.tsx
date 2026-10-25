import type { ReactNode } from "react";

/**
 * Where Claude Code compacted the conversation: a rule across the transcript,
 * as its own terminal marks it, so a reader sees that what came before was
 * summarised rather than kept. `children` is the summary itself, collapsed.
 */
export function CompactionDivider({ children }: { children?: ReactNode }) {
  return (
    <div className="my-3" data-testid="compaction-divider" role="separator" aria-label="Context compacted">
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <span aria-hidden className="h-px flex-1 bg-border" />
        <span className="shrink-0">Context compacted — earlier messages summarised</span>
        <span aria-hidden className="h-px flex-1 bg-border" />
      </div>
      {children}
    </div>
  );
}
