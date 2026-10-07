/**
 * The page's one vocabulary. The huddle ENGINE and the agents keep their exact
 * terms (co-sign, amend, filed, held, roundtable …) — the data and the API are
 * unchanged. This page is for a person seeing a huddle for the first time, so
 * every view says it in plain words, and they all get them from here.
 */
import type { HuddleOutput } from '@/api/huddles'
import type { ArcState, Hold, Resolution, Verdict } from './huddleModel'

// ── names ────────────────────────────────────────────────────────────────────

/** "eva" → "Eva". Agent slugs are names; prose capitalises them. */
export function who(slug: string): string {
  const s = String(slug ?? '').trim()
  return s ? s[0].toUpperCase() + s.slice(1) : ''
}

/** "Ace", "Ace and Eva", "Ace, Echo and Eva". */
export function andList(slugs: string[]): string {
  const names = slugs.map(who).filter(Boolean)
  return names.length <= 1 ? (names[0] ?? '') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/** "Ace's" — never "Ace‘s"; a name ending in s still takes 's (Hal's, Jess's). */
export const possessive = (slug: string) => `${who(slug)}'s`

/** The one line under the title that explains what a huddle is. */
export function huddleExplainer(leader: string): string {
  return `${who(leader)} checked in with the agents to work out what they could push forward for you, alone or together.`
}

// ── steps (the engine's rounds) ──────────────────────────────────────────────

const STEP_NAMES: Record<string, Record<number, string>> = {
  work: { 1: "What everyone's working on", 2: 'Ideas', 3: "Who's in", 4: 'Settling changes' },
}

export function stepName(type: string, round: number): string {
  return STEP_NAMES[type]?.[round] ?? `Step ${round}`
}

/** "Step 2 · Ideas". */
export function stepHeading(type: string, round: number): string {
  const name = stepName(type, round)
  return name === `Step ${round}` ? name : `Step ${round} · ${name}`
}

/** What the leader asks everyone at each step, in a few words (the map's lane). */
const STEP_ASKS: Record<string, Record<number, string>> = {
  work: {
    1: 'what are you working on?',
    2: 'any ideas, alone or together?',
    3: 'are you in?',
    4: 'agree to the changes?',
  },
}

export function stepAsk(type: string, round: number): string {
  return STEP_ASKS[type]?.[round] ?? stepName(type, round).toLowerCase()
}

/** What the leader asked, as a full sentence (the conversation's first bubble). */
const STEP_ASK_SENTENCES: Record<string, Record<number, string>> = {
  work: {
    1: "Tell me what you've been working on and what you think Jonathan's priorities are right now.",
    2: "Here's what everyone said. Suggest ideas — on your own or with teammates — and answer my questions.",
    3: "Teammates suggested ideas that need you. For each one: are you in, in with changes, or not in?",
    4: 'A teammate is in only with changes to your idea — do you agree to them?',
  },
}

export function stepAskSentence(type: string, round: number): string {
  return STEP_ASK_SENTENCES[type]?.[round] ?? ''
}

// ── answers ──────────────────────────────────────────────────────────────────

export const ANSWER_WORDS: Record<ArcState, string> = {
  'co-sign': "I'm in",
  amend: 'In, with changes',
  decline: 'Not in',
  pending: 'No answer yet',
  'amend-accepted': 'Changes agreed',
  'amend-rejected': 'Changes not agreed',
}

export function answerWords(state: ArcState): string {
  return ANSWER_WORDS[state]
}

export function resolutionWords(v: Resolution): string {
  return v === 'accept' ? 'Changes agreed' : 'Changes not agreed'
}

// ── outcome ──────────────────────────────────────────────────────────────────

export const VERDICT_WORDS: Record<Verdict, string> = {
  filed: 'Sent to you to decide',
  held: 'Parked',
  open: 'Still being worked out',
}

/** A board task's status, for the person who decides it. */
export function taskStatusPlain(status: string): string {
  switch (status) {
    case 'suggested': return 'waiting for your yes/no'
    case 'in_progress': return 'in progress'
    case 'done': return 'done'
    case 'declined': return 'you said no'
    default: return status.replace(/_/g, ' ')
  }
}

/** Link text for a board task: "on Eva's board" — never its ticket id. */
export const boardLinkText = (agent: string) => `on ${possessive(agent)} board`

// ── a task's next step: is it actually moving? ───────────────────────────────

const STUCK_PREFIX = /^\s*(?:\[\s*manual\b\]?|blocked\b|waiting\s+on\b|waiting\s+for\b|stuck\b)\s*/i
const STUCK_ANYWHERE = /\bblocked\b|\bnot\s+verifiable\b|\bcan(?:no|')t\s+(?:be\s+)?(?:verif|check|reach|run)/i
const AFTER_AGENT = /\b(?:after|once|when)\s+([A-Z][a-z]+|[a-z]+)\s+(?:reports|ships|fixes|finishes|confirms|says)\b/

export type NextStep = {
  /** The step, lightly de-jargoned ('' when the task has none). */
  text: string
  /** Not moving on its own: blocked, waiting on someone, or manual. */
  stuck: boolean
  /** Stuck on the reader himself. */
  onYou: boolean
  /** Who it waits on, when it names a teammate ("hal"). */
  waitingOn: string
}

/** Words a non-engineer would not know, in plain ones. Light touch only. */
const GLOSSARY: [RegExp, string][] = [
  [/\(([^()]*)\)/g, ''],
  [/~\/\.chrome-sales\/\.sf-creds\.json/gi, 'the Salesforce credentials'],
  [/\.sf-creds\.json/gi, 'the Salesforce credentials'],
  [/\.gws-sa-key\.json/gi, 'the Google Drive key'],
  [/\bcreds\b/gi, 'credentials'],
  [/\bcloud-ec2-\d+\b/gi, 'the cloud runner'],
  [/\bprovisioned\b/gi, 'installed'],
  [/\bprovision\b/gi, 'install'],
  [/\bNOT\s+verifiable\b/g, "can't be checked"],
  [/\bnot\s+verifiable\b/gi, "can't be checked"],
  [/\bfrom cloud runner\b/gi, 'from the cloud runner'],
  [/\bdeps\b/gi, 'dependencies'],
  [/\bSA\b/g, 'service account'],
  [/\bSF\b/g, 'Salesforce'],
  [/\bMCPs?\b/g, 'connectors'],
  [/\bJonathan['’]s\b/g, 'your'],
  [/\bJonathan\b/g, 'you'],
  [/\s+\/\s+/g, ' or '],
  [/\s+\+\s+/g, ' and '],
]

function lightly(s: string): string {
  // A next step is written by the agent to itself: its "your" is the agent's.
  let t = s.replace(/\byour\b/gi, 'its')
  for (const [re, to] of GLOSSARY) t = t.replace(re, to)
  t = stripRefs(t)
  t = t.replace(/\s+([.,;:])/g, '$1').replace(/\s{2,}/g, ' ').trim()
  t = t.replace(/([.!?]\s+)([a-z])/g, (_, a: string, b: string) => a + b.toUpperCase())
  return t ? t[0].toUpperCase() + t.slice(1) : t
}

/** Read a task's `next_action`: what happens next, and whether it is stuck.
 * `agents` are the huddle's members, so "after Hal reports a fix" reads as
 * waiting on Hal. */
export function nextStep(o: Pick<HuddleOutput, 'status'> & { next_action?: string | null }, agents: string[] = []): NextStep {
  const raw = String(o.next_action ?? '').trim()
  const none = { text: '', stuck: false, onYou: false, waitingOn: '' }
  if (!raw || o.status === 'declined' || o.status === 'done') return none
  const mentionsYou = /\bjonathan\b/i.test(raw)
  const prefixed = STUCK_PREFIX.test(raw)
  let body = raw.replace(STUCK_PREFIX, '')
  // "BLOCKED on creds: …" → the reason after the colon reads better on its own.
  if (prefixed) body = body.replace(/^(?:on|by)\s+[^:]{1,30}:\s*/i, () => '')
  const after = AFTER_AGENT.exec(raw)
  const waitingOn = after && agents.map((a) => a.toLowerCase()).includes(after[1].toLowerCase()) ? after[1].toLowerCase() : ''
  const stuck = prefixed || STUCK_ANYWHERE.test(raw) || Boolean(waitingOn)
  let text = lightly(body)
  if (waitingOn && !prefixed) text = `Waiting on ${who(waitingOn)} first — ${text[0].toLowerCase()}${text.slice(1)}`
  return { text, stuck, onYou: stuck && mentionsYou, waitingOn }
}

// ── size and confidence ──────────────────────────────────────────────────────

const EFFORT: Record<string, string> = { xs: 'tiny', s: 'small', m: 'medium', l: 'large', xl: 'very large' }

/** "S" → "small job". */
export function effortWords(effort: string): string {
  const e = String(effort ?? '').trim()
  if (!e) return ''
  const size = EFFORT[e.toLowerCase()] ?? e.toLowerCase()
  return `${size} job`
}

/** 0.6 → "60% sure it's worth it". */
export function confidenceWords(confidence: number | null): string {
  if (confidence === null || confidence === undefined || Number.isNaN(confidence)) return ''
  return `${Math.round(confidence <= 1 ? confidence * 100 : confidence)}% sure it's worth it`
}

export function sizePlain(effort: string, confidence: number | null): string {
  return [effortWords(effort), confidenceWords(confidence)].filter(Boolean).join(' · ')
}

/** "led by Ace, together with Eva" / "Hal, on its own". */
export function peopleWords(lead: string, partners: string[]): string {
  return partners.length ? `led by ${who(lead)}, together with ${andList(partners)}` : `${who(lead)}, on its own`
}

// ── why an idea was parked, and what would un-park it ───────────────────────

export type HoldCtx = { lead: string; proposedBy?: string; leader?: string; open?: boolean }

/** The plain sentence for a structured hold. */
export function holdSentence(h: Hold | null, ctx: HoldCtx): { why: string; clear: string } {
  if (!h) return { why: '', clear: '' }
  const lead = who(ctx.lead)
  const names = andList(h.who)
  const leadRole = !ctx.proposedBy || ctx.proposedBy === ctx.lead ? 'who suggested it' : 'who leads it'
  const leader = who(ctx.leader || 'ada')
  const one = h.who.length === 1
  switch (h.kind) {
    case 'declined':
      return { why: `${names} said ${one ? "they're" : "they're"} not in.`, clear: 'It would need a fresh idea in a later huddle.' }
    case 'amend-rejected':
      return {
        why: `${lead} didn't agree to ${names ? `${possessiveOf(h.who)} changes` : 'the changes asked for'}.`,
        clear: 'It can come back in a later huddle.',
      }
    case 'amend':
      return {
        why: `${names} said yes with changes, and ${lead} (${leadRole}) hasn't confirmed them yet.`,
        clear: `${lead} agreeing to ${one ? `${possessive(h.who[0])}` : 'their'} changes would send it to you.`,
      }
    case 'pending':
      return ctx.open
        ? { why: `Waiting for ${names} to say whether ${one ? "they're" : "they're"} in.`, clear: `${names} saying ${one ? "they're" : "they're"} in.` }
        : { why: `${names} ${one ? "hasn't" : "haven't"} said whether ${one ? "they're" : "they're"} in.`, clear: `${names} saying ${one ? "they're" : "they're"} in, in a later huddle.` }
    case 'gate':
      return {
        why: `Everyone was in, but ${leader} kept it back at the last check (each idea has to serve a stated priority, belong to a project, answer her questions, and fit the limit).`,
        clear: `${possessive(ctx.leader || 'ada')} email says which check it missed.`,
      }
  }
}

function possessiveOf(slugs: string[]): string {
  if (slugs.length === 1) return possessive(slugs[0])
  return `${andList(slugs)}'s`
}

/** Engine words that may leak into free text, in plain ones — used on agent
 * prose shown in the Story so the default view never speaks engine. */
const DEJARGON: [RegExp, string | ((m: string, ...g: string[]) => string)][] = [
  [/\bco-?signing\b/gi, 'signing on'],
  [/\bco-?signed\b/gi, 'signed on'],
  [/\bco-?signs\b/gi, 'signs on'],
  [/\bco-?sign\b/gi, 'sign on'],
  [/\bamendments\b/gi, 'changes'],
  [/\bamendment\b/gi, 'change'],
  [/\bamends\b/gi, 'changes'],
  [/\bamended\b/gi, 'changed'],
  [/\bamending\b/gi, 'changing'],
  [/\bamend\b/gi, 'change'],
  [/\bfiled\b/gi, 'logged'],
  [/\bheld\b/gi, 'parked'],
  [/\broundtable\b/gi, 'idea round'],
  [/\battempts\b/gi, 'tries'],
  [/\battempted\b/gi, 'tried'],
  [/\battempt\b/gi, 'try'],
  // Raw board status codes in agent prose.
  [/\bin_progress\b/g, 'in progress'],
]

function keepCase(src: string, to: string): string {
  return src[0] === src[0].toUpperCase() && src[0] !== src[0].toLowerCase() ? to[0].toUpperCase() + to.slice(1) : to
}

export function deJargon(text: string): string {
  let t = String(text ?? '')
  for (const [re, to] of DEJARGON) t = t.replace(re, (m) => keepCase(m, typeof to === 'string' ? to : to(m)))
  return t
}

/** The engine's own held-reason strings (its email's "Held:" line), rewritten.
 * `who` names the partners when the reason itself does not. */
export function heldReasonWords(raw: string, ctx: HoldCtx & { who?: string[] }): string {
  const r = String(raw ?? '').trim()
  const split = (s: string) => s.split(/\s*,\s*|\s+and\s+/).map((x) => x.trim()).filter(Boolean)
  let m = /^amend(?:ment)?s? unresolved\s*\(([^)]*)\)$/i.exec(r)
  if (m) return holdSentence({ kind: 'amend', who: split(m[1]) }, ctx).why
  m = /^(.+?) (?:has|have) not co-?signed\.?$/i.exec(r)
  if (m) return holdSentence({ kind: 'pending', who: split(m[1]) }, ctx).why
  m = /^(.+?) declined\.?$/i.exec(r)
  if (m) return holdSentence({ kind: 'declined', who: split(m[1]) }, ctx).why
  if (/^amend(?:ment)?s? rejected(?: by (?:the )?lead)?\.?$/i.test(r)) {
    return holdSentence({ kind: 'amend-rejected', who: ctx.who ?? [] }, ctx).why
  }
  const plain = deJargon(r)
  return plain ? plain[0].toUpperCase() + plain.slice(1) + (/[.!?]$/.test(plain) ? '' : '.') : ''
}

// ── shortening agent prose for one-line gists ───────────────────────────────

const TICKET = /(?:\b[a-z][\w-]*#\d+\b|\bPRs?\s*#\d+(?:\/#?\d+)*|#\d+(?:\/#?\d+)*|\b[TP]-?\d+(?:\/[TP]?-?\d+)*\b)/g

/** Drop parentheticals and ticket refs, collapse what is left. */
export function stripRefs(s: string): string {
  let t = String(s ?? '')
  // Nested parentheticals: strip innermost first.
  for (let i = 0; i < 3; i++) t = t.replace(/\s*\([^()]*\)/g, '')
  t = t.replace(TICKET, '')
  t = t.replace(/\s*\/\s*(?=[,;:.]|$)/g, '').replace(/^\s*[/,;:·—-]+\s*/, '')
  t = t.replace(/\s+([,;:.])/g, '$1').replace(/([,;:])(?:\s*[,;:])+/g, '$1').replace(/\s{2,}/g, ' ')
  return t.trim()
}

function cap(s: string, max: number): string {
  if (s.length <= max) return s
  return `${s.slice(0, max).replace(/[\s,;:—-]+\S*$/, '')}…`
}

/** A priority as a short phrase: no parentheticals, refs or "— from the board"
 * provenance tail. */
export function trimPriority(s: string, max = 120): string {
  let t = String(s ?? '')
  t = t.replace(/\s*(?::|—|–|-)\s*from\b[\s\S]*$/i, '')
  t = stripRefs(t)
  t = t.split(/\.\s/)[0].replace(/[.;:,]+$/, '')
  return deJargon(cap(t, max))
}

/** A report line as a one-line gist: drops a "T30 / P-7:" style lead-in, refs
 * and parentheticals, then keeps the first clause. */
export function gistOf(s: string, max = 110): string {
  let t = String(s ?? '')
  // A lead-in made only of refs ("T44 / P-9:") goes.
  t = t.replace(/^\s*(?:[A-Za-z-]*\s*[#-]?\d+[\s/,]*|fleet-sync\s+)+(?:\([^)]*\))?\s*:\s*/i, '')
  t = stripRefs(t)
  t = t.split(/;\s|\.\s|\s—\s|\s–\s/)[0].replace(/[.;:,]+$/, '')
  t = t.replace(/,\s*(?:[a-z]+\s+)?(?:PR\s+)?merged$/i, '')
  return deJargon(cap(t.trim(), max))
}

/** The first sentence of a paragraph, for "Why: …" and a quoted note. */
/** A parenthetical that only carries refs ("(ace#2735, PRs #2737/#2738)"). */
const REF_PAREN = /\s*\([^()]*(?:#\d|\b[TP]-?\d|\bPRs?\b|\d{4}-\d{2}-\d{2})[^()]*\)/g

export function firstSentence(s: string, max = 240, min = 50): string {
  const t = String(s ?? '').replace(REF_PAREN, '').replace(/\s*\(\d+\)\s+/g, ' ').replace(/\s+/g, ' ').trim()
  // Split only where a sentence ends and the next begins — "0.45" stays whole.
  const sentences = t.split(/(?<=[.!?])\s+(?=[A-Z0-9"“(])/)
  let out = ''
  for (const x of sentences) {
    out = `${out} ${x.trim()}`.trim()
    if (out.length >= min) break
  }
  return deJargon(cap(out || t, max))
}

/** The engine's own words, which a reader of the default view should never
 * meet (the Story's tests hold the page to it). */
export const ENGINE_TERMS: RegExp[] = [/\bco-?sign/i, /\bamend/i, /\bfiled\b/i, /\bheld\b/i, /\broundtable\b/i, /\battempt/i]
