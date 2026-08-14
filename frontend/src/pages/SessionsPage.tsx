import { useCallback, useEffect, useState } from "react";

import {
  deleteSession,
  listMySessions,
  rotateSessionToken,
  setSessionVisibility,
  shareUrl,
} from "../api/sessions";
import type { SessionListItem } from "../api/sessions";

export function SessionsPage() {
  const [sessions, setSessions] = useState<SessionListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const load = useCallback(() => {
    listMySessions()
      .then(setSessions)
      .catch(() => setError("Failed to load sessions"));
  }, []);

  useEffect(load, [load]);

  const copy = async (token: string) => {
    await navigator.clipboard.writeText(shareUrl(token));
    setCopied(token);
    setTimeout(() => setCopied(null), 1500);
  };

  const onRotate = async (slug: string) => {
    await rotateSessionToken(slug);
    load();
  };

  const onToggle = async (s: SessionListItem) => {
    await setSessionVisibility(
      s.slug,
      s.visibility === "link" ? "private" : "link",
    );
    load();
  };

  const onDelete = async (slug: string) => {
    if (!confirm("Delete this shared session? The link will stop working.")) return;
    await deleteSession(slug);
    load();
  };

  if (error) return <div className="p-6 text-destructive">{error}</div>;
  if (sessions === null) return <div className="p-6 text-muted-foreground">Loading…</div>;

  return (
    <div className="mx-auto max-w-4xl px-4 py-8">
      <h1 className="text-2xl font-semibold text-foreground">Shared sessions</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Claude Code transcripts shared by you, your agents, and teammates. Run{" "}
        <code className="rounded bg-muted px-1">/canopy:share-session</code> to
        add one.
      </p>

      {sessions.length === 0 ? (
        <p className="mt-8 text-muted-foreground">No shared sessions yet.</p>
      ) : (
        <ul className="mt-6 divide-y divide-border rounded-lg border border-border">
          {sessions.map((s) => (
            <li key={s.slug} className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:gap-4">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate font-medium text-foreground">
                    {s.title || "Claude session"}
                  </span>
                  <span
                    className={`rounded-full px-2 py-0.5 text-xs ${
                      s.visibility === "link"
                        ? "bg-success/10 text-success"
                        : "bg-muted text-muted-foreground"
                    }`}
                  >
                    {s.visibility === "link" ? "link" : "private"}
                  </span>
                  {s.redaction_count > 0 && (
                    <span className="rounded-full bg-warning/10 px-2 py-0.5 text-xs text-warning">
                      {s.redaction_count} redacted
                    </span>
                  )}
                </div>
                <div className="mt-0.5 text-xs text-muted-foreground">
                  {s.message_count} messages ·{" "}
                  {new Date(s.created_at).toLocaleString()}
                  {s.project_slug ? ` · ${s.project_slug}` : ""}
                  {!s.is_owner ? ` · ${s.owner_email}` : ""}
                </div>
              </div>
              <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 text-sm">
                {s.visibility === "link" && s.share_token && (
                  <>
                    <a
                      className="text-muted-foreground underline-offset-2 hover:underline"
                      href={shareUrl(s.share_token)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open
                    </a>
                    <button
                      className="text-muted-foreground hover:text-foreground"
                      onClick={() => copy(s.share_token!)}
                    >
                      {copied === s.share_token ? "Copied!" : "Copy link"}
                    </button>
                    {s.is_owner && (
                      <button
                        className="text-muted-foreground hover:text-foreground"
                        onClick={() => onRotate(s.slug)}
                        title="Invalidate the current link and mint a new one"
                      >
                        Rotate
                      </button>
                    )}
                  </>
                )}
                {s.is_owner && (
                  <>
                    <button
                      className="text-muted-foreground hover:text-foreground"
                      onClick={() => onToggle(s)}
                    >
                      {s.visibility === "link" ? "Make private" : "Make link"}
                    </button>
                    <button
                      className="text-destructive hover:text-destructive/80"
                      onClick={() => onDelete(s.slug)}
                    >
                      Delete
                    </button>
                  </>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
