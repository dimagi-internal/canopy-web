/**
 * Find-in-transcript over the rendered DOM: every case-insensitive match of
 * `query` in the text under `root`, as Ranges. It reads what is on the page —
 * the LOADED messages, including rows folded shut (their text is in the DOM,
 * just hidden) — so older history that has not been fetched is not searched.
 *
 * Highlighting uses the CSS Custom Highlight API, which paints ranges without
 * touching the DOM React owns. Where it is missing the current match is
 * selected instead, so stepping through still shows where you are.
 */

export const HIGHLIGHT_ALL = "canopy-chat-find";
export const HIGHLIGHT_CURRENT = "canopy-chat-find-current";

export function findTextRanges(root: Node, query: string): Range[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];
  const doc = root.ownerDocument ?? (root as Document);
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const ranges: Range[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = (node.nodeValue ?? "").toLowerCase();
    let at = text.indexOf(needle);
    while (at !== -1) {
      const range = doc.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + needle.length);
      ranges.push(range);
      at = text.indexOf(needle, at + needle.length);
    }
  }
  return ranges;
}

interface HighlightRegistry {
  set(name: string, highlight: unknown): void;
  delete(name: string): void;
}

function registry(): { highlights: HighlightRegistry; Highlight: new (...r: Range[]) => unknown } | null {
  const css = (globalThis as { CSS?: { highlights?: HighlightRegistry } }).CSS;
  const Highlight = (globalThis as { Highlight?: new (...r: Range[]) => unknown }).Highlight;
  return css?.highlights && Highlight ? { highlights: css.highlights, Highlight } : null;
}

export function supportsHighlights(): boolean {
  return registry() != null;
}

/** Paint every match, and the current one more strongly. */
export function paintMatches(ranges: Range[], current: number): void {
  const reg = registry();
  if (!reg) return;
  if (ranges.length === 0) {
    clearMatches();
    return;
  }
  reg.highlights.set(HIGHLIGHT_ALL, new reg.Highlight(...ranges));
  const cur = ranges[current];
  if (cur) reg.highlights.set(HIGHLIGHT_CURRENT, new reg.Highlight(cur));
  else reg.highlights.delete(HIGHLIGHT_CURRENT);
}

export function clearMatches(): void {
  const reg = registry();
  if (!reg) return;
  reg.highlights.delete(HIGHLIGHT_ALL);
  reg.highlights.delete(HIGHLIGHT_CURRENT);
}

/** Open every collapsed row around a match and scroll it into view. */
export function revealRange(range: Range): void {
  const start = range.startContainer;
  const el = start.nodeType === Node.ELEMENT_NODE ? (start as Element) : start.parentElement;
  for (let cur: Element | null = el; cur; cur = cur.parentElement) {
    if (cur instanceof HTMLDetailsElement && !cur.open) cur.open = true;
  }
  if (el && typeof el.scrollIntoView === "function") {
    el.scrollIntoView({ block: "center" });
  }
  if (!supportsHighlights()) {
    const sel = globalThis.getSelection?.();
    if (sel) {
      sel.removeAllRanges();
      sel.addRange(range);
    }
  }
}
