import { useEffect, useRef, useState, type RefObject } from "react";
import { ChevronDown, ChevronUp, Search, X } from "lucide-react";

import {
  HIGHLIGHT_ALL,
  HIGHLIGHT_CURRENT,
  clearMatches,
  findTextRanges,
  paintMatches,
  revealRange,
} from "./transcriptFind";

// The two highlight colours, off the theme's tokens so both themes work.
const HIGHLIGHT_CSS = `
::highlight(${HIGHLIGHT_ALL}) { background-color: color-mix(in oklch, var(--warning) 35%, transparent); color: inherit; }
::highlight(${HIGHLIGHT_CURRENT}) { background-color: color-mix(in oklch, var(--primary) 60%, transparent); color: var(--foreground); }
`;

/**
 * A find box over the transcript: highlights every match in the loaded
 * messages and steps through them (Enter / Shift+Enter, or the arrows),
 * opening any folded row a match is in. Esc closes it.
 *
 * `version` changes whenever the transcript does, so matches follow new rows.
 */
export function FindBar({
  rootRef,
  version,
  onClose,
}: {
  rootRef: RefObject<HTMLElement | null>;
  version: unknown;
  onClose: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [ranges, setRanges] = useState<Range[]>([]);
  const [current, setCurrent] = useState(0);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  // Re-find on a new query or a changed transcript. Only a new QUERY jumps to
  // its first match: a reply streaming in must not yank the view about.
  const lastQuery = useRef("");
  useEffect(() => {
    const root = rootRef.current;
    const found = root ? findTextRanges(root, query) : [];
    setRanges(found);
    if (query !== lastQuery.current) {
      lastQuery.current = query;
      setCurrent(0);
      if (found[0]) revealRange(found[0]);
    } else {
      setCurrent((c) => (c < found.length ? c : 0));
    }
  }, [query, version, rootRef]);

  useEffect(() => {
    paintMatches(ranges, current);
  }, [ranges, current]);

  useEffect(() => clearMatches, []);

  const step = (by: number) => {
    if (ranges.length === 0) return;
    const next = (current + by + ranges.length) % ranges.length;
    setCurrent(next);
    revealRange(ranges[next]);
  };

  const hasQuery = query.trim() !== "";
  return (
    <div
      data-testid="chat-find"
      className="flex items-center gap-1.5 border-b border-border bg-background px-3 py-1 text-xs"
    >
      <style>{HIGHLIGHT_CSS}</style>
      <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
      <input
        ref={inputRef}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            step(e.shiftKey ? -1 : 1);
          } else if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            onClose();
          }
        }}
        aria-label="Find in loaded messages"
        placeholder="Find in loaded messages"
        className="min-w-0 flex-1 bg-transparent py-0.5 text-xs text-foreground outline-none placeholder:text-muted-foreground"
      />
      <span className="shrink-0 tabular-nums text-muted-foreground" data-testid="chat-find-count" aria-live="polite">
        {hasQuery
          ? ranges.length === 0
            ? "No matches"
            : `${current + 1} of ${ranges.length}`
          : "loaded messages only"}
      </span>
      <button
        type="button"
        aria-label="Previous match"
        onClick={() => step(-1)}
        disabled={ranges.length === 0}
        className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
      >
        <ChevronUp className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        aria-label="Next match"
        onClick={() => step(1)}
        disabled={ranges.length === 0}
        className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
      >
        <ChevronDown className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        aria-label="Close find"
        onClick={onClose}
        className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
