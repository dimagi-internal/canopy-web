import type { SessionContextFacts, SessionContexts, SessionInfo, TurnContextView, TurnRanAs } from "@/api/chat";
import { sentence } from "./RefusalNotice";
import { Row, Section } from "./SessionInfoRows";
import {
  admissionText,
  audienceLines,
  basisText,
  channelText,
  credentialName,
  decidingStep,
  detailText,
  entryName,
  getsText,
  headline,
  momentText,
  outcomeText,
  participantsText,
  permissionsText,
  profileDetails,
  relationshipText,
  reviewFlags,
  reviewText,
  roleText,
  sensitiveText,
  shortLabel,
  sourceText,
  stepText,
} from "./turnContextWords";

/**
 * The two contexts of the Turn Context design,
 * drawn from what `GET /context` and `GET /turns/{id}/context` serve and nothing else: neither route ever
 * carries a prompt, what canopy knows about a person, or a secret's value, so nothing here can show one.
 */

/** A request's state, as the panel holds it. */
export type Loaded<T> = { state: "loading" } | { state: "error"; message: string } | { state: "done"; data: T };

// --- Session Context ---------------------------------------------------------------------------

const BLOCKED_WHO: Record<string, string> = {
  cannot_write: "People who may only read it",
  not_in_sensitive: "People not in it, or not on its chat",
};

export function SessionContextSection({
  facts,
  binding,
  me,
}: {
  facts: SessionContextFacts;
  binding: SessionInfo["binding"];
  me: number | null;
}) {
  const audience = audienceLines(facts.audience as Record<string, unknown>, me);
  const blocks = facts.send_blocks.map((b) =>
    b.scope === "anyone"
      ? `Nobody: ${String(b.why ?? "")}`
      : BLOCKED_WHO[String(b.code)] ?? `Some people: ${String(b.why ?? "")}`,
  );
  return (
    <Section
      title="Session context"
      note="The facts of this conversation, the same whoever is asking."
      testId="session-context"
    >
      <Row label="Agent" value={facts.agent ?? "none: a person's own repo or project session"} />
      <Row label="Workspace" value={facts.workspace ?? ""} />
      <Row label="Arrives by" value={channelText(facts.channel)} />
      <Row label="Site" value={facts.site} />
      <Row label="Page" value={facts.page} />
      <Row label="Sensitive data" value={sensitiveText(facts.sensitive, facts.sensitive_requirement)} testId="session-context-sensitive" />
      <Row label="Review chat" value={facts.is_review ? "Yes: it reviews one other session" : "No"} />
      <Row
        label="Session it reviews"
        value={facts.review_of ?? ""}
        mono
        copy
        href={facts.review_of ? `/ide/${facts.review_of}` : undefined}
      />
      <Row label="Runner asked for" value={facts.runner_requested ?? ""} mono copy />
      <Row
        label="Runner holding it"
        value={facts.runner_bound ? binding?.runner_name || facts.runner_bound : ""}
        mono={!binding?.runner_name}
      />
      <Row label="Who can see it" value={audience.join("\n")} />
      <Row label="People in it" value={participantsText(facts.participants, me)} />
      <Row label="Who can't send" value={blocks.join("\n") || "nobody is blocked"} />
    </Section>
  );
}

// --- Turn Context: for you, now ------------------------------------------------------------------

export function TurnContextNowSection({ loaded }: { loaded: Loaded<SessionContexts> }) {
  return (
    <Section
      title="Turn context: for you, now"
      note="What would happen if you sent a message now, and why. Rules that check a turn when a runner picks it up are not predicted here."
      testId="turn-context-now"
    >
      {loaded.state === "loading" ? (
        <p className="text-muted-foreground">Loading…</p>
      ) : loaded.state === "error" ? (
        <p className="text-destructive" data-testid="turn-context-error">{loaded.message}</p>
      ) : loaded.data.turn_context ? (
        <TurnContextDetails ctx={loaded.data.turn_context} who="you" />
      ) : (
        <Withheld why={loaded.data.withheld} />
      )}
    </Section>
  );
}

function Withheld({ why }: { why: string }) {
  return (
    <div className="rounded border border-border bg-muted p-2" data-testid="turn-context-withheld">
      <p className="font-medium text-foreground">Withheld</p>
      <p className="text-muted-foreground">{why || "You may not see what a turn here may do."}</p>
    </div>
  );
}

/** One Turn Context: the answer, its reason beside each value, and the steps that decided it. */
export function TurnContextDetails({ ctx, who }: { ctx: TurnContextView; who: "you" | "they" }) {
  const s = ctx.standing;
  const refused = ctx.access === "refused";
  return (
    <div className="space-y-2" data-testid="turn-context">
      {refused ? (
        <Refused ctx={ctx} who={who} />
      ) : (
        <p className="font-medium text-foreground" data-testid="turn-context-headline">{headline(ctx, who)}</p>
      )}
      <dl className="space-y-0.5">
        {!refused && <Row label="Why" value={basisText(ctx.basis, s.source)} />}
        <Row label="Who" value={relationshipText(s.relationship)} />
        {/* The owner, an admin and canopy itself are their own reason: no role or route to repeat. */}
        <Row label="Role" value={s.agent_role === s.relationship ? "" : roleText(s.agent_role)} />
        <Row label="Through" value={["owner", "admin", "system"].includes(s.source) ? "" : s.via || sourceText(s.source)} />
        <Row label="Holds" value={permissionsText(s.permissions)} />
        {ctx.profile && profileDetails(ctx.profile).map((r) => <Row key={r.label} label={r.label} value={r.value} />)}
        <Row
          label="Kept apart"
          value={ctx.session_key_suffix ? "Yes: its own conversation, so it can't see the whole agent's history" : ""}
        />
        {/* A refused turn never runs: no approval to speak of, and nothing it gets, said once. */}
        <Row label="Approval" value={refused ? "" : reviewText(ctx.review)} />
        <Row label="Also" value={refused ? "" : reviewFlags(ctx.review)} />
        <Row label="What it gets" value={refused ? "nothing: a refused turn does not run" : ""} />
      </dl>
      {!refused && ctx.credentials.length > 0 && (
        <div>
          <p className="mb-0.5 text-muted-foreground">What it gets</p>
          <dl className="space-y-0.5" data-testid="turn-context-credentials">
            {ctx.credentials.map((c) => (
              <Row key={c.name} label={credentialName(c.name)} value={c.why ? `${getsText(c.gets)}: ${c.why}` : getsText(c.gets)} />
            ))}
          </dl>
        </div>
      )}
      <Trace ctx={ctx} />
    </div>
  );
}

function Refused({ ctx, who }: { ctx: TurnContextView; who: "you" | "they" }) {
  const r = ctx.refusal;
  return (
    <div className="rounded border border-destructive/30 bg-destructive/10 p-2" data-testid="turn-context-refused">
      <p className="font-medium text-destructive">{headline(ctx, who)}</p>
      {r && (
        <dl className="mt-1 space-y-0.5">
          {/* The server's words (apps/agents/refusals.py); `why` starts lower-case to follow "not run: ". */}
          <Row label="Why not" value={sentence(r.why)} testId="turn-context-why-not" />
          <Row label="How to fix it" value={r.how_to_fix} testId="turn-context-how-to-fix" />
          <Row label="Who can fix it" value={r.who_can_fix} testId="turn-context-who-can-fix" />
        </dl>
      )}
    </div>
  );
}

function Trace({ ctx }: { ctx: TurnContextView }) {
  if (!ctx.trace.length) return null;
  const decided = decidingStep(ctx);
  return (
    <div>
      <p className="mb-0.5 text-muted-foreground">How canopy decided</p>
      <ol className="space-y-0.5" data-testid="turn-context-trace">
        {ctx.trace.map((e, i) => {
          const detail = detailText(e);
          const deciding = i === decided;
          return (
            <li
              key={`${i}-${e.rule}`}
              data-deciding={deciding || undefined}
              className={`grid grid-cols-[1.25rem_1fr] gap-1 rounded px-1 py-0.5 ${
                deciding ? "border border-primary/40 bg-primary/10" : ""
              }`}
            >
              <span className="tabular-nums text-muted-foreground">{i + 1}.</span>
              <span className="min-w-0 break-words">
                <span className={deciding ? "font-medium text-foreground" : "text-foreground-secondary"}>{stepText(e.rule)}</span>
                <span className="text-muted-foreground">: </span>
                <span className={deciding ? "font-medium text-foreground" : ""}>{outcomeText(e)}</span>
                {detail && <span className="text-muted-foreground"> ({detail})</span>}
                {deciding && <span className="ml-1 text-[11px] font-semibold text-primary">decided it</span>}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// --- Turn Context: one turn ----------------------------------------------------------------------

/** What one turn ran as, opened with the turn. "this turn ran as X; today it would be Y" when they differ. */
export function TurnRanAsView({ loaded }: { loaded: Loaded<TurnRanAs> | undefined }) {
  if (!loaded || loaded.state === "loading") {
    return <p className="text-muted-foreground" data-testid="turn-ran-as-loading">Loading what it ran as…</p>;
  }
  if (loaded.state === "error") {
    return <p className="text-destructive" data-testid="turn-ran-as-error">{loaded.message}</p>;
  }
  const r = loaded.data;
  const today = r.now ? shortLabel(r.now) : "";
  return (
    <div className="space-y-1" data-testid="turn-ran-as" data-status={r.status}>
      {r.status === "withheld" ? (
        <Withheld why={r.note} />
      ) : r.status === "not_recorded" || r.status === "unavailable" || !r.ran_as ? (
        <dl className="space-y-0.5">
          <Row
            label="Ran as"
            value={
              r.status === "unavailable"
                ? `not recorded: ${r.note || "canopy could not record what this turn ran as"}`
                : "not recorded (canopy did not keep this for this turn)"
            }
            testId="turn-ran-as-not-recorded"
          />
          <Row label="Today it would be" value={today} />
        </dl>
      ) : (
        <Recorded r={r} ranAs={r.ran_as} today={today} />
      )}
    </div>
  );
}

function Recorded({ r, ranAs, today }: { r: TurnRanAs; ranAs: NonNullable<TurnRanAs["ran_as"]>; today: string }) {
  const ran = shortLabel(ranAs);
  return (
    <>
      {r.differs ? (
        <p className="rounded border border-warning/30 bg-warning/10 p-2 text-foreground" data-testid="turn-ran-as-differs">
          This turn ran as <strong>{ran}</strong>; today it would be <strong>{today}</strong>.
        </p>
      ) : (
        <dl className="space-y-0.5">
          <Row label="Ran as" value={ran} />
          <Row label="Today" value={today ? "the same" : ""} />
        </dl>
      )}
      <dl className="space-y-0.5">
        <Row
          label="Recorded"
          value={`${momentText(ranAs.moment)}${ranAs.runner ? `, on ${ranAs.runner.name}` : ""}`}
        />
        <Row label="It asked for" value={ranAs.requested ? entryName(ranAs.requested) : ""} />
        <Row label="Rules after" value={admissionText(ranAs.admission)} />
      </dl>
      <details className="rounded border border-border">
        <summary className="cursor-pointer px-2 py-1 text-muted-foreground hover:bg-muted">What it ran as, and why</summary>
        <div className="border-t border-border p-2">
          <TurnContextDetails ctx={ranAs} who="they" />
        </div>
      </details>
      {r.differs && r.now && (
        <details className="rounded border border-border">
          <summary className="cursor-pointer px-2 py-1 text-muted-foreground hover:bg-muted">What it would be today, and why</summary>
          <div className="border-t border-border p-2">
            <TurnContextDetails ctx={r.now} who="they" />
          </div>
        </details>
      )}
    </>
  );
}
