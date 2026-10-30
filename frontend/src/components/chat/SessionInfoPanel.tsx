import { useEffect, useState, type ReactNode } from "react";
import { getSessionInfo, type SessionInfo, type SessionInfoTurn } from "@/api/chat";
import { relativeAge } from "@/lib/relativeAge";
import { CopyButton } from "canopy-ui/ui";
import { cliLabel } from "@/lib/clis";

/**
 * "Session info", opened from the chat's ⋯ menu and the /ide row menu:
 * everything canopy stores about a session, read from one route
 * (`GET /api/canopy-sessions/{id}/info`).
 *
 * It exists to answer "where did this session come from?" without reading a
 * runner log. A dispatched session's name is only the slug of its brief
 * (`c-cc-ace-…`), and who sent it, through which credential and client, under
 * which parent and on which runner were all stored but shown nowhere.
 *
 * The sections are the questions in the order you ask them: what is it, who
 * started it, where it runs, what it did, the turns that drove it. "Raw" shows
 * and copies the whole record, so nothing stored is out of reach. Ids are
 * shown whole and copyable, because the usual next step is pasting one
 * somewhere else.
 */
export function SessionInfoPanel({ sessionId }: { sessionId: string }) {
  const [info, setInfo] = useState<SessionInfo | null>(null);
  const [error, setError] = useState("");
  const [raw, setRaw] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getSessionInfo(sessionId)
      .then((r) => !cancelled && setInfo(r))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : "Could not load session info."));
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  if (error) return <p className="text-[12px] text-destructive">{error}</p>;
  if (!info) return <p className="text-[12px] text-muted-foreground">Loading…</p>;

  const s = info.session;
  const b = info.binding;
  // The turn that started it: the oldest one listed when the list is complete.
  const first = info.turn_count <= info.turns.length ? info.turns[info.turns.length - 1] : undefined;
  const prov = (s.provenance ?? {}) as Record<string, unknown>;
  const activity = (s.activity ?? {}) as Record<string, unknown>;

  return (
    <div data-testid="session-info" className="max-h-[70vh] space-y-4 overflow-y-auto pr-1 text-[12px]">
      <div className="flex justify-end gap-2">
        <button
          type="button"
          className="rounded border border-border px-2 py-0.5 text-[11px] hover:bg-muted"
          onClick={() => setRaw((r) => !r)}
          data-testid="session-info-raw-toggle"
        >
          {raw ? "Formatted" : "Raw"}
        </button>
        <CopyButton text={JSON.stringify(info, null, 2)} variant="outline" resetMs={1200} title="Copy everything">Copy all</CopyButton>
      </div>

      {raw ? (
        <pre data-testid="session-info-raw" className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono text-[11px]">
          {JSON.stringify(info, null, 2)}
        </pre>
      ) : (
        <>
          <Section title="Session">
            <Row label="Title" value={s.title || "(untitled)"} />
            <Row label="Id" value={s.id} mono copy />
            <Row label="With" value={s.agent_slug ? `agent ${s.agent_slug}` : s.project ? `repo ${s.project}` : "—"} />
            <Row label="Workspace" value={s.workspace} />
            <Row label="Status" value={s.status} />
            <Row label="Origin" value={originText(s)} />
            <Row label="Mode" value={s.turn_mode ? `${s.turn_mode}, started by ${s.turn_origin || "?"}` : ""} />
            <Row label="Created" value={when(s.created_at)} />
            <Row label="Last activity" value={when(s.last_activity_at)} />
            <Row label="Updated" value={when(info.updated_at)} />
            <Row label="Messages" value={String(info.message_count)} />
            <Row label="People" value={String(info.participant_count)} />
            {Object.keys(info.metadata ?? {}).length > 0 && (
              <Row label="Metadata" value={JSON.stringify(info.metadata)} mono />
            )}
          </Section>

          <Section title="Where it came from">
            <Row label="Opened by" value={s.created_by ?? info.contact ?? (first ? "" : "a runner (no person)")} />
            {first && (
              <>
                <Row label="Asked by" value={initiatorText(first)} />
                <Row label="Sent as" value={first.enqueued_by_email ?? ""} />
                <Row label="Via" value={`${first.origin}${first.initiator.via ? ` · ${first.initiator.via}` : ""}`} />
                <Row label="Verified" value={first.initiator.assurance} />
              </>
            )}
            <Row label="Credential" value={credentialText(prov.credential ?? first?.provenance?.credential)} />
            <Row label="Client" value={str(prov.client ?? first?.provenance?.client)} />
            <Row label="User agent" value={str(prov.user_agent ?? first?.provenance?.user_agent)} />
            <Row label="Request id" value={str(prov.request_id ?? first?.provenance?.request_id)} mono copy />
            <Row label="MCP tool" value={str(prov.mcp_tool ?? first?.provenance?.mcp_tool)} />
            <Row label="Parent session" value={s.parent_session_id ?? ""} mono copy href={s.parent_session_id ? `/ide/${s.parent_session_id}` : undefined} />
            <Row label="Parent turn" value={s.parent_turn_id ?? ""} mono copy />
            <Row label="Parent task" value={s.parent_task} mono copy />
            <Row label="Parent Claude session" value={s.parent_claude_session} mono copy />
            {prov.parent != null && <Row label="Parent (as sent)" value={JSON.stringify(prov.parent)} mono />}
            {first?.prompt && <Row label="Brief" value={first.prompt.slice(0, 400) + (first.prompt.length > 400 ? "…" : "")} />}
          </Section>

          <Section title="Where it runs">
            {s.cli && (
              <div data-testid="session-info-cli">
                <Row label="Agent CLI" value={cliLabel(s.cli)} />
              </div>
            )}
            {b ? (
              <>
                <Row
                  label="Runner"
                  value={b.runner_name ? `${b.runner_name}${b.runner_kind ? ` (${b.runner_kind})` : ""} · ${s.runner_status ?? "?"}` : "unbound"}
                />
                <Row label="Host" value={b.host} />
                <Row label="emdash project" value={b.emdash_project} mono />
                <Row label="emdash task" value={b.session_key} mono copy />
                <Row label="Thread key" value={b.thread_key} mono copy />
                <Row label="Claude session" value={b.transcript_id} mono copy />
                <Row label="Task uid" value={b.task_uid} mono copy />
                <Row label="Board task" value={b.agent_task_ext_id} mono />
                <Row label="Runner status" value={[b.status, b.agent_status].filter(Boolean).join(" · ")} />
                <Row label="Summary" value={b.summary} />
                <Row label="Moved from" value={b.transferred_from_name ? `${b.transferred_from_name}, ${when(b.transferred_at)}` : ""} />
                <Row label="Last reported" value={when(b.reported_at)} />
                <Row label="Last seen live" value={when(b.live_seen_at)} />
                <Row label="Last interacted" value={when(b.last_interacted_at)} />
              </>
            ) : s.turn_runner_name ? (
              <Row label="Runner" value={`${s.turn_runner_name} · ran its turns, not bound to it`} />
            ) : (
              <p className="text-muted-foreground">No runner has backed this session yet.</p>
            )}
            {s.slack && <Row label="Slack" value={s.slack.channel} href={s.slack.app_url || s.slack.url || undefined} />}
          </Section>

          {Object.keys(activity).length > 0 && (
            <Section title="What it did">
              {Object.entries(activity).map(([k, v]) => (
                <Row key={k} label={k} value={activityText(v)} mono />
              ))}
            </Section>
          )}

          <Section title={`Turns (${info.turn_count > info.turns.length ? `latest ${info.turns.length} of ${info.turn_count}` : info.turn_count})`}>
            {info.turns.length === 0 ? (
              <p className="text-muted-foreground">No turn has driven this session.</p>
            ) : (
              <ul className="space-y-1">
                {info.turns.map((t) => (
                  <TurnRow key={t.id} turn={t} />
                ))}
              </ul>
            )}
          </Section>
        </>
      )}
    </div>
  );
}

function TurnRow({ turn: t }: { turn: SessionInfoTurn }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="rounded border border-border">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-2 py-1 text-left hover:bg-muted"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className="text-muted-foreground">{open ? "▾" : "▸"}</span>
        <span className="shrink-0 tabular-nums text-muted-foreground" title={t.created_at}>{relativeAge(t.created_at)}</span>
        <span className="shrink-0">{t.origin}</span>
        <span className="shrink-0 text-muted-foreground">{t.status}</span>
        <span className="truncate">{initiatorText(t)}</span>
      </button>
      {open && (
        <dl className="space-y-0.5 border-t border-border px-2 py-1">
          <Row label="Turn id" value={t.id} mono copy />
          <Row label="Asked by" value={initiatorText(t)} />
          <Row label="Sent as" value={t.enqueued_by_email ?? ""} />
          <Row label="Verified" value={t.initiator.assurance} />
          <Row label="Credential" value={credentialText(t.provenance?.credential)} />
          <Row label="Client" value={str(t.provenance?.client)} />
          <Row label="Mode" value={t.turn_mode ? `${t.turn_mode}${t.turn_mode_basis ? ` (${t.turn_mode_basis})` : ""}` : ""} />
          <Row label="Runner" value={t.claimed_by_name ?? ""} />
          <Row label="Claude session" value={t.session_id} mono copy />
          <Row label="Claimed" value={when(t.claimed_at)} />
          <Row label="Finished" value={when(t.finished_at)} />
          <Row label="Parent session" value={t.parent_session_id ?? ""} mono copy href={t.parent_session_id ? `/ide/${t.parent_session_id}` : undefined} />
          <Row label="Parent turn" value={t.parent_turn_id ?? ""} mono copy />
          <Row label="Parent Claude session" value={t.parent_claude_session} mono copy />
          {t.content_hidden ? (
            <p className="text-muted-foreground">Its content is hidden: you may not read this turn's prompt.</p>
          ) : (
            <>
              <Row label="Prompt" value={t.prompt} />
              <Row label="Result" value={t.result_note} />
            </>
          )}
        </dl>
      )}
    </li>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{title}</h3>
      <dl className="space-y-0.5">{children}</dl>
    </section>
  );
}

/** One fact. An empty value is not rendered: the panel lists what IS stored. */
function Row({
  label,
  value,
  mono,
  copy,
  href,
}: {
  label: string;
  value: string;
  mono?: boolean;
  copy?: boolean;
  href?: string;
}) {
  if (!value) return null;
  return (
    <div className="grid grid-cols-[8.5rem_1fr] items-start gap-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={`flex min-w-0 items-start gap-1 ${mono ? "font-mono text-[11px]" : ""}`}>
        {href ? (
          <a href={href} className="break-all text-primary hover:underline">{value}</a>
        ) : (
          <span className="whitespace-pre-wrap break-all">{value}</span>
        )}
        {copy && <CopyButton text={value} variant="ghost" className="shrink-0" title="Copy value" resetMs={1200} />}
      </dd>
    </div>
  );
}

function when(iso: string | null | undefined): string {
  if (!iso) return "";
  return `${new Date(iso).toLocaleString()} (${relativeAge(iso)})`;
}

/** `origin` says which side created the row (canopy-web, or a runner that found it), not where
 *  the conversation began: a Slack or email caller's session is a web row. */
export function originText(s: { origin: string; turn_origin?: string }): string {
  if (s.origin === "runner") return "runner (discovered or dispatched)";
  if (s.turn_origin === "slack") return "web (opened from Slack)";
  if (s.turn_origin === "email") return "web (opened from email)";
  return "web (started in canopy)";
}

function str(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : JSON.stringify(v);
}

/** {type, id, label} → "pat 47 · DIM-SI-W144"; a browser session reads "browser session". */
export function credentialText(v: unknown): string {
  if (!v || typeof v !== "object") return "";
  const c = v as Record<string, unknown>;
  if (c.type === "session") return "browser session";
  return [c.type, c.id, c.label].filter((x) => x !== null && x !== undefined && x !== "").join(" · ");
}

/** Who a turn was for: a person, a contact, or an agent. */
export function initiatorText(t: SessionInfoTurn): string {
  const i = t.initiator;
  const person = i.user ?? i.contact;
  if (person) return person.name && person.name !== person.email ? `${person.name} <${person.email}>` : person.email;
  if (i.agent) return `agent ${i.agent}`;
  return i.kind || "";
}

function activityText(v: unknown): string {
  if (Array.isArray(v)) return v.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join("\n");
  return str(v);
}
