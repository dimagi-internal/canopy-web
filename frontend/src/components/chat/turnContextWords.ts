/**
 * The Turn Context and Session Context in plain words, for the session panel.
 *
 * The routes speak in the rule's own terms (`whole_agent`, `capability:connect`,
 * `trusted_without_a_list`). The person reading the panel is the agent's owner,
 * not an engineer, so every term is turned into a short phrase here, in one
 * place, and the raw value stays one click away under "Raw". Anything this file
 * does not know is shown as the server sent it rather than hidden.
 *
 * Design: docs/superpowers/specs/2026-11-05-turn-context-design.md § 5.
 */
import type { TurnContextView } from "@/api/chat";

type Profile = NonNullable<TurnContextView["profile"]>;
type Trace = TurnContextView["trace"][number];

/** The two names canopy's read mode is published under. A profile named either is read mode. */
const READ_MODE_NAMES = new Set(["ask", "read"]);

export function isReadModeName(name: string | null | undefined): boolean {
  return !!name && READ_MODE_NAMES.has(name);
}

/** A profile's own name as a person reads it: read mode is never shown by its stored name. */
export function entryName(name: string | null | undefined): string {
  if (!name) return "";
  return isReadModeName(name) ? "read mode" : `"${name}"`;
}

/** What a turn may do, in a few words: the X and Y of "this turn ran as X; today it would be Y". */
export function shortLabel(ctx: Pick<TurnContextView, "access" | "profile">): string {
  const p = ctx.profile;
  if (ctx.access === "refused" || !p) return "refused";
  switch (p.kind) {
    case "whole_agent":
      return "the whole agent";
    case "read_mode":
      return "read mode";
    case "connected_site":
      return p.site ? `${entryName(p.name)} on ${p.site}` : entryName(p.name);
    case "session_review":
      return "review only";
    case "deny_all":
      return "nothing (blocked)";
    case "no_agent":
      return "a session with no agent";
    default:
      return p.kind;
  }
}

/** The one-line answer at the top of a Turn Context. */
export function headline(ctx: Pick<TurnContextView, "access" | "profile">, who: "you" | "they" = "you"): string {
  const p = ctx.profile;
  if (ctx.access === "refused" || !p) return who === "you" ? "You can't send a message here." : "Refused: it does not run.";
  switch (p.kind) {
    case "whole_agent":
      return who === "you" ? "You can use the whole agent." : "The whole agent.";
    case "read_mode":
      return "Read mode only: it can look things up and answer, within read mode's limits.";
    case "connected_site":
      return `Only what ${entryName(p.name)} allows${p.site ? `, for ${p.site}` : ""}.`;
    case "session_review":
      return "Review only: it reads the one session it was asked to review, and nothing else.";
    case "deny_all":
      return "Nothing: what it was let in as no longer exists, so canopy blocks everything.";
    case "no_agent":
      return "No agent: this is a person's own repo or project session.";
    default:
      return p.kind;
  }
}

/** `basis`: why, in one word on the wire, in a short phrase here. */
export function basisText(basis: string, source = ""): string {
  if (!basis) return "";
  const [head, ...rest] = basis.split(":");
  const tail = rest.join(":");
  switch (head) {
    case "owner":
      return "the agent's owner";
    case "admin":
      return "an admin of the agent";
    case "system":
      return "canopy itself, or the agent's own login";
    case "session":
      return `${article(tail)} ${tail} in a session the agent's owner or an admin started`;
    case "grant":
      return "a line on the agent's access list";
    case "capability":
      // A claim words every confined turn this way, an owner's too: only a list line is "from the access list".
      if (source && !source.startsWith("list:")) return `it was let in as ${entryName(tail)}`;
      return isReadModeName(tail) ? "read mode, from the access list" : `the access list opens ${entryName(tail)}`;
    case "sensitive":
      return `the conversation holds sensitive data, so it is held to ${entryName(tail)}`;
    case "refused":
      return "refused";
    case "no-interface":
      return "no agent";
    default:
      return basis;
  }
}

function article(word: string): string {
  return /^[aeiou]/i.test(word) ? "an" : "a";
}

const RELATIONSHIP: Record<string, string> = {
  owner: "Owner of the agent",
  admin: "Admin of the agent",
  member: "Signed in to canopy",
  contact: "Outside canopy (an email or site visitor)",
  system: "Canopy or an agent",
};

export function relationshipText(rel: string): string {
  return RELATIONSHIP[rel] ?? rel;
}

const ROLE: Record<string, string> = {
  owner: "Owner",
  admin: "Admin",
  editor: "Editor",
  read_write: "Read and write",
  read_only: "Read only",
  system: "Canopy",
};

export function roleText(role: string | null | undefined): string {
  if (!role) return "";
  return ROLE[role] ?? role;
}

const PERMISSION_ORDER = ["read", "write", "agent_edit"];

/** Permissions in the order they build on each other, then the connected sites. */
export function permissionsText(ps: readonly string[]): string {
  const rank = (p: string) => (PERMISSION_ORDER.includes(p) ? PERMISSION_ORDER.indexOf(p) : PERMISSION_ORDER.length);
  return [...ps].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b)).map(permissionText).join(", ");
}

/** A permission as a person reads it: `use:<name>` is "use <name>". */
export function permissionText(p: string): string {
  if (p === "read") return "read";
  if (p === "write") return "write";
  if (p === "agent_edit") return "change its settings";
  if (p === "use:*") return "use any connected site";
  if (p.startsWith("use:")) return `use ${entryName(p.slice(4))}`;
  return p;
}

/** Where the standing came from, when the server gave no words of its own (`via`). */
export function sourceText(source: string): string {
  if (!source) return "";
  if (source === "owner") return "being the agent's owner";
  if (source === "admin") return "being an admin of the agent";
  if (source === "system") return "being canopy itself";
  if (source === "session") return "being in this session";
  if (source.startsWith("list:")) return "the agent's access list";
  return source;
}

const CREDENTIAL: Record<string, string> = {
  canopy_login: "Agent's canopy login",
  github_identity: "GitHub identity",
  caller_token: "Canopy token, as the asker",
  agent_vault: "Agent's own secrets",
};

export function credentialName(name: string): string {
  return CREDENTIAL[name] ?? name;
}

export function getsText(gets: string): string {
  if (gets === "yes") return "Yes";
  if (gets === "no") return "No";
  if (gets === "pending") return "Decided when a runner picks it up";
  return gets;
}

/** Turn mode, as the approval it means. */
export function reviewText(review: TurnContextView["review"]): string {
  const mode =
    review.mode === "manual"
      ? "Manual: anything it sends waits for approval"
      : review.mode === "auto"
        ? "Auto: it acts within its rails, reviewed afterwards"
        : review.mode === "pending"
          ? "Decided when a runner picks it up"
          : review.mode == null
            ? "None (no agent)"
            : review.mode;
  return review.basis ? `${mode} (set by ${review.basis})` : mode;
}

export function reviewFlags(review: TurnContextView["review"]): string {
  return [
    review.manual_only && "always manual",
    review.may_request_auto && "can switch it to auto",
    review.may_pin_runner && "can choose the runner",
  ]
    .filter(Boolean)
    .join(", ");
}

/** A list from a profile: null means nothing is listed (the whole agent), empty means none. */
export function listText(list: readonly string[] | null | undefined): string {
  if (list == null) return "";
  return list.length === 0 ? "none" : list.join(", ");
}

export function profileDetails(p: Profile): { label: string; value: string }[] {
  if (p.kind === "whole_agent") return [{ label: "May use", value: "everything the agent has" }];
  if (p.kind === "no_agent") return [{ label: "May use", value: "what the person's own session has" }];
  const rows = [
    { label: "Limited to", value: p.kind === "read_mode" ? "read mode" : entryName(p.name) },
    { label: "Site", value: p.site ?? "" },
    { label: "About it", value: p.description ?? "" },
    { label: "Tools", value: listText(p.tools) || (p.kind === "deny_all" ? "none" : "") },
    { label: "Shell commands", value: listText(p.bash) },
    { label: "Can read files", value: listText(p.read_paths) },
    { label: "Can write files", value: listText(p.write_paths) },
    { label: "Data it handles", value: p.data === "sensitive" ? "sensitive data" : p.data ?? "" },
  ];
  return rows;
}

// --- the trace ----------------------------------------------------------------------------------

const STEP: Record<string, string> = {
  send_blocks: "Can this session take a message",
  trusted_without_a_list: "Owner, admin or canopy",
  session_grants: "Extra access from being in this session",
  access_list: "The agent's access list",
  entry: "What their access opens",
  review_chat: "Review chat",
  sensitive: "Sensitive data",
  no_agent: "No agent",
  stamp: "What the turn was let in as",
  blocked_sender: "Blocked sender",
};

export function stepText(rule: string): string {
  return STEP[rule] ?? rule;
}

/** What a step answered. Profile names come through `entryName` so read mode reads as read mode. */
export function outcomeText(e: Trace): string {
  const o = e.outcome;
  if (e.rule === "trusted_without_a_list") {
    if (o === "no") return "no";
    if (o === "owner") return "yes, the owner";
    if (o === "admin") return "yes, an admin";
    if (o === "system") return "yes, canopy itself";
  }
  if (e.rule === "access_list" && o !== "holds nothing") {
    return `holds ${o.split(",").map(permissionText).join(", ")}`;
  }
  if (e.rule === "entry" && o !== "not open") return `opens ${entryName(o)}`;
  if (e.rule === "review_chat" && o === "session_review") return "review only";
  if (e.rule === "sensitive" && !["not sensitive", "no change", "refused"].includes(o)) return `held to ${entryName(o)}`;
  if (e.rule === "stamp") {
    if (o === "whole_agent") return "the whole agent";
    if (o === "read_mode") return "read mode";
    if (o === "connected_site") return "a connected site";
    if (o === "session_review") return "review only";
    if (o === "deny_all") return "nothing (it no longer exists)";
  }
  if (e.rule === "send_blocks" && o === "none") return "yes";
  return o.replace(/_/g, " ");
}

/** A step's detail, with any stored read-mode name read as read mode. */
export function detailText(e: Trace): string {
  const d = e.detail;
  if (!d) return "";
  if (e.rule === "stamp") return d ? `let in as ${entryName(d)}` : "let in as the whole agent";
  const m = /^(named|page or site|read mode): (.*)$/.exec(d);
  if (m) {
    const how = m[1] === "read mode" ? "nothing named, so" : m[1] === "named" ? "named" : "chosen by the page or site:";
    return `${how} ${entryName(m[2])}`;
  }
  // "relationship member": the panel's "Who" row already says it.
  if (d.startsWith("relationship ")) return "";
  if (d.startsWith("grant:")) return "";
  return d;
}

/** Steps that ask a question and change nothing when the answer is no. */
const PASSES = new Set(["not sensitive", "no change", "none", "no", "already refused"]);

/**
 * Which step decided the answer, by index: the trace ends at the deciding rule, but rules that
 * only narrow run after it and change nothing when they don't apply. A refusal is decided by the
 * step that refused: a send block whose code is the refusal's, else the last step that said no.
 */
export function decidingStep(ctx: Pick<TurnContextView, "trace" | "refusal">): number {
  const trace = ctx.trace;
  if (ctx.refusal) {
    const code = ctx.refusal.code;
    const blocked = trace.findIndex((e) => e.rule === "send_blocks" && e.outcome === code);
    if (blocked >= 0) return blocked;
    for (let i = trace.length - 1; i >= 0; i--) {
      const e = trace[i];
      if (e.outcome === "refused" || e.outcome === "holds nothing" || e.outcome === "not open") return i;
    }
  }
  for (let i = trace.length - 1; i >= 0; i--) {
    if (!PASSES.has(trace[i].outcome)) return i;
  }
  return trace.length - 1;
}

// --- the Session Context --------------------------------------------------------------------------

const CHANNEL: Record<string, string> = {
  canopy_web_chat: "canopy chat",
  api: "the API",
  ace_web: "ace-web",
  canopy_scheduler: "a schedule",
  email: "email",
  slack: "Slack",
  local: "a person's own computer",
};

export function channelText(channel: string): string {
  if (!channel) return "no message yet";
  return CHANNEL[channel] ?? channel;
}

export function sensitiveText(sensitive: boolean, requirement: string): string {
  if (!sensitive) return "No";
  if (requirement === "sensitive_data") return "Yes: it holds a site's sensitive data";
  return "Yes: its settings could not be read, so canopy treats it as sensitive";
}

const LEG: Record<string, [string, string]> = {
  creator: ["started it", "started it"],
  participants: ["is in it", "are in it"],
  box_owner: ["owns the runner it is on", "own the runner it is on"],
  agent_admins: ["is an admin of the agent", "are admins of the agent"],
  own_login: ["is the agent's own login", "are the agent's own logins"],
  coaches: ["is a coach reviewing it", "are coaches reviewing it"],
  asked_reviewers: ["was asked to review it", "were asked to review it"],
  runner_admins: ["administers its runner", "administer its runner"],
};

function idsOf(v: unknown): unknown[] {
  if (Array.isArray(v)) return v;
  if (v && typeof v === "object") return Object.keys(v);
  return [];
}

function people(n: number, youIn: boolean): string {
  if (youIn) return n === 1 ? "you" : `you and ${n - 1} other${n - 1 === 1 ? "" : "s"}`;
  return n === 1 ? "1 person" : `${n} people`;
}

/** Who can see it, one line per kind of reader that has anyone. Ids stay in Raw. */
export function audienceLines(audience: Record<string, unknown>, me: number | null): string[] {
  const out: string[] = [];
  for (const [leg, value] of Object.entries(audience ?? {})) {
    if (leg === "project_teams") {
      for (const [team, ids] of Object.entries((value ?? {}) as Record<string, unknown>)) {
        const list = idsOf(ids);
        if (list.length) out.push(`${people(list.length, isMe(list, me))} on the ${team} project`);
      }
      continue;
    }
    const list = idsOf(value);
    if (!list.length) continue;
    const [one, many] = LEG[leg] ?? [leg, leg];
    const youIn = isMe(list, me);
    out.push(`${people(list.length, youIn)} ${list.length === 1 && !youIn ? one : many}`);
  }
  return out;
}

function isMe(ids: unknown[], me: number | null): boolean {
  return me != null && ids.some((id) => String(id) === String(me));
}

/** `(user, role)` rows as "you (owner), 2 others (editor, viewer)". */
export function participantsText(rows: readonly Record<string, unknown>[], me: number | null): string {
  if (!rows.length) return "nobody added";
  const mine = rows.find((r) => me != null && String(r.user_id) === String(me));
  const others = rows.filter((r) => r !== mine);
  const parts: string[] = [];
  if (mine) parts.push(`you (${String(mine.role)})`);
  if (others.length) {
    const roles = [...new Set(others.map((r) => String(r.role)))].join(", ");
    const k = others.length;
    const who = mine ? `${k} other${k === 1 ? "" : "s"}` : k === 1 ? "1 person" : `${k} people`;
    parts.push(`${who} (${roles})`);
  }
  return parts.join(", ");
}

/** A session grant: who it is for, and the reason the server gives in words. */
export function grantText(g: Record<string, unknown>, me: number | null): string {
  const who = me != null && String(g.user_id) === String(me) ? "You" : "One person";
  const source = typeof g.source === "string" && g.source ? `: ${g.source}` : "";
  return `${who} can use the whole agent here${source}`;
}

export function sendBlockText(b: Record<string, unknown>): string {
  const scope = b.scope === "anyone" ? "Nobody can send" : "Some people can't send";
  return `${scope}: ${String(b.why ?? "")}`;
}

// --- a turn's record ------------------------------------------------------------------------------

export function momentText(moment: string): string {
  if (moment === "claim") return "when a runner picked it up";
  if (moment === "enqueue") return "when it was refused on arrival";
  if (moment === "open") return "when the session opened";
  return moment;
}

/** What an admission rule did after the Turn Context let the turn through. */
export function admissionText(admission: Record<string, unknown> | null | undefined): string {
  if (!admission) return "";
  const summary = typeof admission.summary === "string" ? admission.summary : "";
  if (admission.outcome === "rejected") return `Then stopped by a rule${summary ? `: ${summary}` : ""}`;
  if (admission.outcome === "raised_to_auto") return `A rule switched it to auto${summary ? `: ${summary}` : ""}`;
  return String(admission.outcome ?? "");
}
