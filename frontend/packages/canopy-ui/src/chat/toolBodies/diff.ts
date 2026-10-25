/**
 * Line-level diffs for the file-editing tools (Edit / MultiEdit / Write), the
 * way Claude Code shows them: removed lines, added lines, unchanged context.
 *
 * Dependency-free so a vitest run needs no DOM.
 */

export interface DiffLine {
  kind: "ctx" | "del" | "add";
  text: string;
}

export interface DiffStats {
  added: number;
  removed: number;
}

/** Above this many cells the LCS table is skipped: the changed middle is shown
 *  as all-removed then all-added, which is still correct, just less tidy. */
const LCS_CELL_LIMIT = 250_000;

function splitLines(s: string): string[] {
  if (s === "") return [];
  const lines = s.split("\n");
  // A trailing newline is the end of the last line, not an empty line after it.
  if (lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/** A line diff of `before` → `after`: common prefix and suffix as context, the
 *  middle by longest common subsequence. */
export function lineDiff(before: string, after: string): DiffLine[] {
  const a = splitLines(before);
  const b = splitLines(after);
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }
  const out: DiffLine[] = a.slice(0, start).map((text) => ({ kind: "ctx", text }));
  const midA = a.slice(start, endA);
  const midB = b.slice(start, endB);
  out.push(...middleDiff(midA, midB));
  out.push(...a.slice(endA).map((text) => ({ kind: "ctx" as const, text })));
  return out;
}

function middleDiff(a: string[], b: string[]): DiffLine[] {
  const n = a.length;
  const m = b.length;
  if (n === 0 || m === 0 || n * m > LCS_CELL_LIMIT) {
    return [
      ...a.map((text) => ({ kind: "del" as const, text })),
      ...b.map((text) => ({ kind: "add" as const, text })),
    ];
  }
  // lcs[i][j] = LCS length of a[i:] and b[j:], flattened.
  const w = m + 1;
  const lcs = new Uint32Array((n + 1) * w);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i * w + j] =
        a[i] === b[j]
          ? lcs[(i + 1) * w + j + 1] + 1
          : Math.max(lcs[(i + 1) * w + j], lcs[i * w + j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ kind: "ctx", text: a[i] });
      i++;
      j++;
    } else if (lcs[(i + 1) * w + j] >= lcs[i * w + j + 1]) {
      out.push({ kind: "del", text: a[i++] });
    } else {
      out.push({ kind: "add", text: b[j++] });
    }
  }
  while (i < n) out.push({ kind: "del", text: a[i++] });
  while (j < m) out.push({ kind: "add", text: b[j++] });
  return out;
}

export function diffStats(lines: DiffLine[]): DiffStats {
  let added = 0;
  let removed = 0;
  for (const l of lines) {
    if (l.kind === "add") added++;
    else if (l.kind === "del") removed++;
  }
  return { added, removed };
}

/** One edit of a file: what was there, what replaces it. */
export interface FileEdit {
  oldString: string;
  newString: string;
  replaceAll: boolean;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function toEdit(raw: unknown): FileEdit | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.old_string !== "string" && typeof r.new_string !== "string") {
    return null;
  }
  return {
    oldString: str(r.old_string),
    newString: str(r.new_string),
    replaceAll: r.replace_all === true,
  };
}

/** The edits an Edit / MultiEdit / Write call makes, or null for any other
 *  tool (or an input too malformed to read). A Write is one edit from "". */
export function fileEdits(name: string, input: unknown): FileEdit[] | null {
  if (!input || typeof input !== "object") return null;
  const i = input as Record<string, unknown>;
  if (name === "Edit") {
    const e = toEdit(i);
    return e ? [e] : null;
  }
  if (name === "MultiEdit") {
    if (!Array.isArray(i.edits)) return null;
    return i.edits.map(toEdit).filter((e): e is FileEdit => e !== null);
  }
  if (name === "Write") {
    if (typeof i.content !== "string") return null;
    return [{ oldString: "", newString: i.content, replaceAll: false }];
  }
  return null;
}

/** `+A −R` for a file-editing call, summed over its edits; null otherwise. */
export function editStats(name: string, input: unknown): DiffStats | null {
  const edits = fileEdits(name, input);
  if (edits === null) return null;
  const total = { added: 0, removed: 0 };
  for (const e of edits) {
    const s = diffStats(lineDiff(e.oldString, e.newString));
    total.added += s.added;
    total.removed += s.removed;
  }
  return total;
}
