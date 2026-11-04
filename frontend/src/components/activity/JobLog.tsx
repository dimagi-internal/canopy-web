import { useRef } from "react";
import { getTurn } from "@/api/harness";
import { listTurnEvents } from "@/api/turns";
import { usePolled } from "@/hooks/usePolled";

/** How much of a job's output stays in view: its last 1 MB, counted in characters. */
export const JOB_LOG_VIEW_CHARS = 1024 * 1024;
const REFRESH_MS = 3000;
const STILL_GOING = new Set(["queued", "claimed", "running", "needs_human"]);
const isGoing = (status: string | null | undefined) => STILL_GOING.has(status ?? "");

/** The tail of `text`, without half a surrogate pair at its start. */
function lastOf(text: string): string {
  const tail = text.slice(-JOB_LOG_VIEW_CHARS);
  const first = tail.charCodeAt(0);
  return first >= 0xdc00 && first <= 0xdfff ? tail.slice(1) : tail;
}

/** What a plain job printed, as a plain log, and how it ended. A job runs no model, so its
 * turn has no transcript and no chat: its output arrives as `status` events carrying
 * `{source: "job", text}` (the box streams them), and `result_note` holds the exit and the
 * last lines. Nothing here asks for a transcript or offers a reply box.
 *
 * The events are pages of 500, read from where the last read stopped, so a long log costs the
 * new lines only. They are refreshed while the job runs and read to the end once it has
 * finished. The text is what the box sent, joined as it was sent: a batch ends wherever the
 * box's buffer did, often mid-line, so a break between batches would be invented. */
export function JobLog({ turnId, resultNote, status }: { turnId: string; resultNote: string; status: string | null | undefined }) {
  // Kept across polls, not in state: a refresh that fails part-way resumes where it stopped.
  const log = useRef({ turnId, after: 0, text: "", cut: false });

  const { data, error } = usePolled(
    async () => {
      if (log.current.turnId !== turnId) log.current = { turnId, after: 0, text: "", cut: false };
      const seen = log.current;
      // The turn first: whatever it printed before it ended is then read below.
      const going = isGoing(status);
      const turn = going ? await getTurn(turnId) : null;
      for (;;) {
        const page = await listTurnEvents(turnId, seen.after);
        const end = page[page.length - 1]?.seq;
        if (end === undefined || end <= seen.after) break;
        seen.after = end;
        const added = page
          .filter((e) => e.kind === "status" && e.payload?.source === "job")
          .map((e) => String(e.payload?.text ?? ""))
          .join("");
        seen.text += added;
        if (seen.text.length > JOB_LOG_VIEW_CHARS) {
          seen.text = lastOf(seen.text);
          seen.cut = true;
        }
      }
      return { text: seen.text, cut: seen.cut,
               status: turn?.status ?? status, note: turn?.result_note ?? resultNote };
    },
    { ms: (last) => (isGoing(last.data?.status ?? status) ? REFRESH_MS : null), deps: [turnId] },
  );

  const note = (data?.note ?? resultNote ?? "").trim();
  const going = isGoing(data?.status ?? status);
  return (
    <div className="space-y-2">
      {data === undefined ? (
        error ? (
          <p className="text-xs text-destructive">{error.message || "Failed to load the output"}</p>
        ) : (
          <p className="text-xs text-muted-foreground">Loading output…</p>
        )
      ) : data.text === "" ? (
        <p className="text-xs text-muted-foreground">{going ? "Nothing printed yet." : "The job printed nothing."}</p>
      ) : (
        <>
          {data.cut && (
            <p className="text-xs text-muted-foreground">Earlier output was cut: this shows the last 1 MB.</p>
          )}
          <pre
            data-testid="job-log"
            className="max-h-[32rem] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-muted p-3 font-mono text-[12px] text-foreground"
          >
            {data.text}
          </pre>
        </>
      )}
      {note && (
        <pre className="whitespace-pre-wrap break-words font-mono text-[12px] text-foreground-secondary">{note}</pre>
      )}
    </div>
  );
}
