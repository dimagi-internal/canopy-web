import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { ApiError, getShared } from "../api/sessions";
import type { SharedSection, SharedView } from "../api/sessions";
import { MessageList } from "../components/transcript/MessageList";

type LoadState =
  | { kind: "loading" }
  | { kind: "loaded"; view: SharedView }
  | { kind: "error"; code: string; message: string };

/** "Jun 18, 2026" — or "Jun 18 – Jun 20, 2026" when it spans days. */
function formatWhen(start: string | null, end: string | null): string {
  if (!start) return "";
  const s = new Date(start);
  const full: Intl.DateTimeFormatOptions = { month: "short", day: "numeric", year: "numeric" };
  if (!end) return s.toLocaleDateString(undefined, full);
  const e = new Date(end);
  if (s.toDateString() === e.toDateString()) return s.toLocaleDateString(undefined, full);
  return `${s.toLocaleDateString(undefined, { month: "short", day: "numeric" })} – ${e.toLocaleDateString(undefined, full)}`;
}

/** "2h 31m" / "45m" from a second count. */
function formatSeconds(secs: number): string {
  const mins = Math.round(secs / 60);
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return h ? `${h}h ${m}m` : `${m}m`;
}

/**
 * "How long" — prefer the idle-capped active duration; fall back to the
 * wall-clock first→last span for older uploads without active_seconds.
 */
function formatDuration(
  start: string | null,
  end: string | null,
  activeSeconds: number | null,
): string {
  if (activeSeconds && activeSeconds > 0) return formatSeconds(activeSeconds);
  if (start && end) {
    const ms = new Date(end).getTime() - new Date(start).getTime();
    if (ms > 0) return formatSeconds(ms / 1000);
  }
  return "";
}

/** "Jun 18, 2026 · 2h 31m · 41 turns" — a session's properties. */
function propsLine(
  start: string | null,
  end: string | null,
  turns: number,
  activeSeconds: number | null,
): string {
  const parts: string[] = [];
  const when = formatWhen(start, end);
  if (when) parts.push(when);
  const dur = formatDuration(start, end, activeSeconds);
  if (dur) parts.push(dur);
  parts.push(`${turns} turn${turns === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

const PAGE = "min-h-screen bg-white";
const SHELL = "mx-auto max-w-3xl px-4 py-8";

/**
 * Public, read-only view of a shared Claude session — or a multi-session arc.
 *
 * For an arc, the landing view is a LIST of the member sessions (each with its
 * properties); clicking one opens just that session's turn-synthesis, with a
 * "back to all sessions" link. (No endless single scroll.)
 *
 * Mounted OUTSIDE AppLayout so anonymous (non-dimagi) visitors can read it
 * without the app chrome's authenticated calls bouncing them to login.
 *
 * Renders exactly what was uploaded — the uploader reduces transcripts to a
 * clean conversation (prompts + final replies) client-side by default.
 */
export default function SessionSharePage() {
  // `workspace` is set on /w/:workspace/share/:token — the API confirms the
  // share lives there (see FlatArtifactRedirect for the flat route).
  const { token = "", workspace } = useParams();
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    if (!token) return;
    setSelected(null);
    getShared(token, workspace)
      .then((view) => setState({ kind: "loaded", view }))
      .catch((e) => {
        if (e instanceof ApiError) {
          setState({ kind: "error", code: e.code, message: e.message });
        } else {
          setState({ kind: "error", code: "unknown", message: "Failed to load" });
        }
      });
  }, [token, workspace]);

  if (state.kind === "loading") {
    return (
      <div className={`flex items-center justify-center ${PAGE}`}>
        <p className="text-zinc-500">Loading shared session…</p>
      </div>
    );
  }

  if (state.kind === "error") {
    const message =
      state.code === "not-found"
        ? "This share link is invalid, has expired, or was revoked."
        : state.message;
    return (
      <div className={`flex items-center justify-center ${PAGE}`}>
        <p className="text-lg font-medium text-zinc-700">{message}</p>
      </div>
    );
  }

  const { view } = state;

  // --- Single session ------------------------------------------------------
  if (view.kind === "session") {
    return (
      <div className={PAGE}>
        <div className={SHELL}>
          <h1 className="text-xl font-semibold text-zinc-900">
            {view.title || "Claude session"}
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-500">
            <span>Shared session — read only</span>
            <span className="text-zinc-300" aria-hidden>·</span>
            <span>{propsLine(view.started_at, view.ended_at, view.turn_count, view.active_seconds)}</span>
            {view.redaction_count > 0 && (
              <RedactionBadge count={view.redaction_count} />
            )}
          </div>
          <div className="mt-6">
            <MessageList messages={view.messages} />
          </div>
        </div>
      </div>
    );
  }

  // --- Arc: detail (one session) ------------------------------------------
  const sections = view.sections;
  if (selected !== null && sections[selected]) {
    const s = sections[selected];
    return (
      <div className={PAGE}>
        <div className={SHELL}>
          <button
            type="button"
            onClick={() => setSelected(null)}
            className="text-sm text-zinc-500 hover:text-zinc-800"
          >
            ← All sessions ({sections.length})
          </button>
          <div className="mt-4 border-b border-zinc-200 pb-3">
            <div className="flex items-baseline gap-3">
              <span className="text-xs font-medium text-zinc-400">
                {selected + 1}/{sections.length}
              </span>
              <h1 className="text-lg font-semibold text-zinc-900">
                {s.heading || `Session ${selected + 1}`}
              </h1>
            </div>
            <p className="mt-1 pl-8 text-xs text-zinc-500">
              {propsLine(s.started_at, s.ended_at, s.turn_count, s.active_seconds)}
              {s.redaction_count > 0 &&
                ` · ${s.redaction_count} secret${s.redaction_count === 1 ? "" : "s"} redacted`}
            </p>
          </div>
          <div className="mt-6">
            <MessageList messages={s.messages} />
          </div>
          <div className="mt-10 flex justify-between border-t border-zinc-100 pt-4 text-sm">
            <button
              type="button"
              disabled={selected === 0}
              onClick={() => setSelected(selected - 1)}
              className="text-zinc-500 enabled:hover:text-zinc-800 disabled:opacity-30"
            >
              ← Previous
            </button>
            <button
              type="button"
              disabled={selected === sections.length - 1}
              onClick={() => setSelected(selected + 1)}
              className="text-zinc-500 enabled:hover:text-zinc-800 disabled:opacity-30"
            >
              Next →
            </button>
          </div>
        </div>
      </div>
    );
  }

  // --- Arc: session list (landing) ----------------------------------------
  return (
    <div className={PAGE}>
      <div className={SHELL}>
        <h1 className="text-xl font-semibold text-zinc-900">
          {view.title || "Claude session arc"}
        </h1>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-500">
          <span>
            Shared arc · {sections.length} session{sections.length === 1 ? "" : "s"} — read only
          </span>
          <span className="text-zinc-300" aria-hidden>·</span>
          <span>{propsLine(view.started_at, view.ended_at, view.turn_count, view.active_seconds)}</span>
          {view.redaction_count > 0 && <RedactionBadge count={view.redaction_count} />}
        </div>

        <ol className="mt-6 divide-y divide-zinc-100 rounded-lg border border-zinc-200">
          {sections.map((section: SharedSection, i: number) => (
            <li key={i}>
              <button
                type="button"
                onClick={() => setSelected(i)}
                className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-zinc-50"
              >
                <span className="mt-0.5 w-6 shrink-0 text-xs font-medium text-zinc-400">
                  {i + 1}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium text-zinc-800">
                    {section.heading || `Session ${i + 1}`}
                  </span>
                  <span className="mt-0.5 block text-xs text-zinc-500">
                    {propsLine(section.started_at, section.ended_at, section.turn_count, section.active_seconds)}
                  </span>
                </span>
                <span className="mt-0.5 shrink-0 text-zinc-300" aria-hidden>›</span>
              </button>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

function RedactionBadge({ count }: { count: number }) {
  return (
    <span
      className="rounded-full bg-amber-50 px-2 py-0.5 text-amber-700"
      title="Best-effort secret scrub. Not a guarantee — review before sharing widely."
    >
      {count} secret{count === 1 ? "" : "s"} redacted
    </span>
  );
}
