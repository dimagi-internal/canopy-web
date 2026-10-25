import { type ReactNode, useState } from "react";

import type { Message } from "../protocol";
import { type DiffLine, type FileEdit, fileEdits, lineDiff } from "./diff";

/**
 * The expanded body of one tool call, shaped per tool the way Claude Code
 * shows it: a diff for a file edit, a terminal block for Bash, a readable
 * key/value list for the tools whose input is a handful of named fields — and
 * the raw JSON only for a tool nothing here knows.
 */
interface Props {
  name: string;
  input: unknown;
  result: Message | null;
  isError: boolean;
}

/** Tools whose input reads best as a short list of named fields. Every
 *  `mcp__…` tool joins them. */
const KEY_VALUE_TOOLS = new Set([
  "Read",
  "Grep",
  "Glob",
  "WebFetch",
  "WebSearch",
  "Skill",
  "Agent",
  "Task",
  "NotebookEdit",
  "LS",
]);

/** Field order in the key/value view: what a call is about comes first. */
const KEY_ORDER = [
  "file_path",
  "path",
  "notebook_path",
  "pattern",
  "glob",
  "query",
  "url",
  "skill",
  "args",
  "subagent_type",
  "description",
  "prompt",
];

/** Lines a diff shows before "show all N lines". */
export const DIFF_LINE_CAP = 24;

export function ToolBody({ name, input, result, isError }: Props) {
  const edits = fileEdits(name, input);
  if (edits !== null) {
    return (
      <EditBody
        name={name}
        input={input as Record<string, unknown>}
        edits={edits}
        result={result}
        isError={isError}
      />
    );
  }
  if (name === "Bash" && isRecord(input) && typeof input.command === "string") {
    return <BashBody input={input} result={result} isError={isError} />;
  }
  const keyValue =
    isRecord(input) && (KEY_VALUE_TOOLS.has(name) || name.startsWith("mcp__"));
  return (
    <div className="space-y-2">
      {input !== undefined &&
        (keyValue ? (
          <KeyValueInput input={input as Record<string, unknown>} />
        ) : (
          <div>
            <Label>input</Label>
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded bg-background p-2 font-mono text-xs">
              {JSON.stringify(input, null, 2)}
            </pre>
          </div>
        ))}
      <ResultBlock result={result} isError={isError} />
    </div>
  );
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

function Label({ children }: { children: ReactNode }) {
  return (
    <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
      {children}
    </div>
  );
}

function ResultBlock({
  result,
  isError,
  label = "result",
}: {
  result: Message | null;
  isError: boolean;
  label?: string;
}) {
  return (
    <div>
      <Label>{label}</Label>
      {result === null ? (
        <div className="text-xs italic text-muted-foreground">(running…)</div>
      ) : (
        <pre
          data-testid="tool-result"
          data-error={isError || undefined}
          className={`max-h-96 overflow-auto whitespace-pre-wrap break-all rounded p-2 font-mono text-xs ${
            isError
              ? "border border-destructive/30 bg-destructive/10 text-destructive"
              : "bg-background"
          }`}
        >
          {result.plaintext || (
            <span className="italic text-muted-foreground">(no output)</span>
          )}
        </pre>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Edit / MultiEdit / Write
// ---------------------------------------------------------------------------

function EditBody({
  name,
  input,
  edits,
  result,
  isError,
}: {
  name: string;
  input: Record<string, unknown>;
  edits: FileEdit[];
  result: Message | null;
  isError: boolean;
}) {
  const path = typeof input.file_path === "string" ? input.file_path : "";
  return (
    <div className="space-y-2" data-testid="tool-diff">
      {path && (
        <div className="break-all font-mono text-xs text-foreground">{path}</div>
      )}
      {edits.map((e, i) => (
        <div key={i}>
          {(edits.length > 1 || e.replaceAll) && (
            <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
              {edits.length > 1 && `edit ${i + 1} of ${edits.length}`}
              {edits.length > 1 && e.replaceAll && " · "}
              {e.replaceAll && "replace all occurrences"}
            </div>
          )}
          <DiffBlock
            lines={lineDiff(e.oldString, e.newString)}
            empty={name === "Write" ? "(empty file)" : "(no change)"}
          />
        </div>
      ))}
      {/* "The file … has been updated successfully" says nothing the diff
          doesn't; only a failure is worth the space. */}
      {(isError || result === null) && (
        <ResultBlock result={result} isError={isError} />
      )}
    </div>
  );
}

const LINE_STYLE: Record<DiffLine["kind"], string> = {
  ctx: "text-muted-foreground",
  del: "bg-destructive/10 text-destructive",
  add: "bg-success/10 text-success",
};
const LINE_SIGN: Record<DiffLine["kind"], string> = { ctx: " ", del: "-", add: "+" };

export function DiffBlock({
  lines,
  empty = "(no change)",
}: {
  lines: DiffLine[];
  empty?: string;
}) {
  const [showAll, setShowAll] = useState(false);
  if (lines.length === 0) {
    return <div className="text-xs italic text-muted-foreground">{empty}</div>;
  }
  const capped = !showAll && lines.length > DIFF_LINE_CAP;
  const shown = capped ? lines.slice(0, DIFF_LINE_CAP) : lines;
  return (
    <div className="overflow-hidden rounded border border-border/60 bg-background">
      <div className="max-h-[32rem] overflow-auto">
        <pre className="min-w-max py-1 font-mono text-xs leading-5">
          {shown.map((l, i) => (
            <div
              key={i}
              data-diff={l.kind}
              className={`flex px-2 ${LINE_STYLE[l.kind]}`}
            >
              <span className="w-4 shrink-0 select-none opacity-70">
                {LINE_SIGN[l.kind]}
              </span>
              <span className="whitespace-pre">{l.text || " "}</span>
            </div>
          ))}
        </pre>
      </div>
      {lines.length > DIFF_LINE_CAP && (
        <button
          type="button"
          onClick={() => setShowAll((v) => !v)}
          className="w-full border-t border-border/60 px-2 py-1 text-left text-xs text-muted-foreground hover:bg-muted/60 hover:text-foreground"
        >
          {showAll ? "show fewer lines" : `show all ${lines.length} lines`}
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bash
// ---------------------------------------------------------------------------

function BashBody({
  input,
  result,
  isError,
}: {
  input: Record<string, unknown>;
  result: Message | null;
  isError: boolean;
}) {
  const description =
    typeof input.description === "string" ? input.description : "";
  return (
    <div className="space-y-2" data-testid="tool-bash">
      {description && (
        <div className="text-xs text-muted-foreground">{description}</div>
      )}
      <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded border border-border/60 bg-background p-2 font-mono text-xs text-foreground">
        <span className="select-none text-muted-foreground">$ </span>
        {String(input.command)}
      </pre>
      <ResultBlock result={result} isError={isError} label="output" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Key/value input
// ---------------------------------------------------------------------------

function keyRank(k: string): number {
  const i = KEY_ORDER.indexOf(k);
  return i === -1 ? KEY_ORDER.length : i;
}

function KeyValueInput({ input }: { input: Record<string, unknown> }) {
  const entries = Object.entries(input)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .sort(([a], [b]) => keyRank(a) - keyRank(b));
  if (entries.length === 0) return null;
  return (
    <dl
      data-testid="tool-input-fields"
      className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs"
    >
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="pt-px font-mono text-muted-foreground">{k}</dt>
          <dd className="min-w-0">
            <FieldValue value={v} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function FieldValue({ value }: { value: unknown }) {
  if (typeof value === "string") {
    if (value.includes("\n") || value.length > 160) {
      return (
        <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words rounded bg-background px-2 py-1 font-mono text-xs text-foreground">
          {value}
        </pre>
      );
    }
    return <span className="break-all font-mono text-foreground">{value}</span>;
  }
  if (
    Array.isArray(value) &&
    value.every((v) => typeof v === "string" || typeof v === "number")
  ) {
    return (
      <span className="break-all font-mono text-foreground">
        {value.join(", ")}
      </span>
    );
  }
  if (typeof value === "object") {
    return (
      <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-all rounded bg-background p-2 font-mono text-xs text-foreground">
        {JSON.stringify(value, null, 2)}
      </pre>
    );
  }
  return <span className="font-mono text-foreground">{String(value)}</span>;
}
