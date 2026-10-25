import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";

import { foldLine, type FileChange, type TurnSummary } from "./turnWork";

/** Each file a turn changed, with the lines its edits added and removed. */
export function FilesChanged({ files }: { files: FileChange[] }) {
  if (files.length === 0) return null;
  return (
    <div data-testid="files-changed" className="rounded border border-border bg-card px-2.5 py-1.5 text-xs">
      <div className="mb-1 text-[11px] font-medium text-muted-foreground">
        {files.length} file{files.length === 1 ? "" : "s"} changed
      </div>
      <ul className="space-y-0.5">
        {files.map((f) => (
          <li key={f.path} className="flex min-w-0 items-center gap-2 font-mono">
            <span className="w-10 shrink-0 text-right tabular-nums text-success">+{f.added}</span>
            <span className="w-10 shrink-0 tabular-nums text-destructive">−{f.removed}</span>
            <span className="min-w-0 truncate text-foreground" title={f.path}>
              {f.path}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * A finished turn's work as ONE line — "Worked for 4m 12s · 23 tool calls ·
 * 3 files changed" — that opens to the detailed rows. The reply itself is
 * rendered by the caller, in full, below this.
 */
export function TurnFold({
  summary,
  forceOpen,
  children,
}: {
  summary: TurnSummary;
  forceOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <details data-testid="turn-fold" open={forceOpen} className="group/fold mr-auto w-full text-sm">
      <summary className="flex w-fit cursor-pointer items-center gap-1.5 rounded px-1.5 py-0.5 text-xs text-muted-foreground hover:bg-muted/60 hover:text-foreground select-none [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-3 w-3 shrink-0 transition-transform group-open/fold:rotate-90" />
        <span data-testid="turn-fold-line">{foldLine(summary)}</span>
        {summary.errors > 0 && (
          <span className="text-destructive">
            · {summary.errors} error{summary.errors === 1 ? "" : "s"}
          </span>
        )}
      </summary>
      <div className="mt-1 flex flex-col gap-2 border-l-2 border-border pl-3">
        <FilesChanged files={summary.files} />
        {children}
      </div>
    </details>
  );
}
