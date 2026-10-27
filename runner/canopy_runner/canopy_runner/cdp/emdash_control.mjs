// emdash CDP control sidecar — drives the RUNNING emdash app over the Chrome
// DevTools Protocol (emdash is Electron; launch it with --remote-debugging-port).
// This is the sanctioned path (no DB injection, no app patching): tasks created
// here flow through emdash's own UI, so they appear live in the sidebar and run
// interactive `claude` on the subscription. Commands (arg = JSON on stdin, or argv[3]):
//   list                              -> {ok, tasks:[names], projects:[names]}
//   create {project, prompt}          -> {ok, action:"created"}    (new session)
//   open-send {task, text}            -> {ok, action:"sent", task} (REUSE existing)
//                                        Every task lookup (open-send, interrupt, read-term,
//                                        send-keys) also takes `project`: task names are only
//                                        unique per project, so with it the row is looked up
//                                        under that project's sidebar section (sidebar_section.mjs).
//                                        -> {ok, action:"collision", line} if the prompt
//                                           already holds UNSENT text (human was typing when
//                                           emdash switched tasks) — does NOT clobber it.
//   open-send {task, text, clearFirst}-> {ok, action:"sent-cleared"} kills the current input
//                                           line first, then sends (the human's "Clear & send").
//   interrupt {task}                  -> {ok, task} opens the task (same lookup as open-send)
//                                           and presses Escape — Claude Code's TUI treats
//                                           this as "stop the running turn" (see runner.cancel).
//   composer {task, project}          -> {ok, found, text, typed, screen, running} READ-ONLY look at
//                                        the task's composer and rendered frame, for the runner's
//                                        delivery recovery (execute._recover_delivery) — never types.
//   add-project {name, path}          -> {ok, action:"submitted"} makes the git checkout at
//                                        `path` the emdash project `name` via the Add Project
//                                        dialog (the runner verifies it against emdash's DB).
//   close-task {task, project}        -> {ok, action:"deleted"|"absent"} DELETES the task
//                                        from emdash (delete is the designed close behaviour).
//                                        Verifies it is gone before reporting success; "absent"
//                                        means it already was.
// Text is delivered via CDP Input.insertText (one atomic commit, not char-by-char
// typing) so it lands fast and narrows the window for a keystroke collision.
// All output is a single JSON line on stdout. Occlusion-proof: uses JS-dispatched
// clicks so it works while emdash is backgrounded (no foreground focus needed).
import { chromium } from 'playwright-core';
import { isSidebarLabel, pickInSection } from './sidebar_section.mjs';
import { asTyped } from './typed_text.mjs';
import { readArgs } from './sidecar_args.mjs';
import { draftWithoutOurs, hasForeignLead, squash } from './composer_check.mjs';


// WHICH terminal is "the" terminal. Injected as source into page.evaluate calls
// so focusing and reading can never disagree about it.
//
// The old rule — first element with width > 0 — admitted everything: measured on
// a live emdash, 17 .xterm nodes ALL passed it, of which 16 were 16x16 ghosts
// parked off-screen (top: -617) for other tasks. It picked the right one only
// because DOM order happened to put it first, which is luck, not a rule. That
// selector also drives open-send and interrupt, so a wrong pick types a human's
// message — or a menu answer — into someone else's pane.
//
// Size is the honest signal: a mounted-but-hidden pane is tiny and off-screen, a
// real one fills the pane. Among genuinely-sized terminals (a split, or a shell
// tab beside the agent) prefer the one that looks like a CLAUDE session, since
// that is the only one where a prompt or a menu answer means anything; fall back
// to the largest.
// NOTE: every use below wraps this INSIDE a single arrow-IIFE. `page.evaluate(str)`
// evaluates ONE EXPRESSION, so prefixing a function DECLARATION produced
// `function(){}(...)()` — a call on a function expression — and every call failed
// with "(intermediate value) is not a function".
// String.raw on every one of these, and it is load-bearing. A plain template
// literal PROCESSES escapes before Playwright ever sees the source: `\n` became a
// real newline (so `.join('\n')` produced an unterminated string —
// "SyntaxError: Invalid or unexpected token" at runtime, nowhere near this file)
// and `\s` collapsed to a literal `s`, silently turning every `\s*` in these
// regexes into "zero or more letter s". The file still parsed; only the evaluated
// string was wrong, which is why `node --check` was clean and the collision guard
// quietly never matched.
const ACTIVE_TERM_FN = String.raw`
function activeTerm() {
  const real = [...document.querySelectorAll('.xterm')].filter(t => {
    if (t.offsetParent === null) return false;
    const r = t.getBoundingClientRect();
    return r.width > 200 && r.height > 200 && r.bottom > 0 && r.right > 0;
  });
  if (!real.length) return null;
  const byArea = real.slice().sort((a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return (rb.width * rb.height) - (ra.width * ra.height);
  });
  if (byArea.length === 1) return byArea[0];
  const claudeish = byArea.find(t => {
    const rows = t.querySelector('.xterm-rows');
    const text = rows ? rows.textContent || '' : '';
    return /[⏺✻⎿]/.test(text) || /esc to interrupt|shift\\+tab to cycle/i.test(text);
  });
  return claudeish || byArea[0];
}
`;

// WHERE the composer is, structurally — not "the last rows". The input line is
// the `❯` row sitting directly UNDER a box rule (with the closing rule below and
// the status bar under that); historical `❯` user messages in scrollback never
// have that rule. The scan covers the WHOLE viewport, top-bounded by nothing and
// bottom-bounded by the status bar when one is rendered, because a FRESH session
// draws its TUI at the TOP of the pane (measured live, #521: composer at row 13
// of 38, rows 24–37 all empty) — any "last N rows" window reads exactly those
// sessions as empty and blind-appends. Returns {found, text}:
//   found:false  = no composer in the rendered frame (mid-redraw, a menu is up,
//                  or a clipped stale frame) — the caller must NOT treat this as
//                  "empty and safe to send"; it cannot see what a send would hit.
//   found:true   = composer visible; `text` is its content ('' = genuinely empty;
//                  NBSP — what an empty composer actually holds after ❯ — is
//                  normalized to space before trimming).
const COMPOSER_FN = String.raw`
function composerText(rows) {
  const norm = rows.map(r => (r || '').replace(/\u00a0/g, ' '));
  const isRule = (s) => /^\s*[╭╰]?─{8,}[╮╯]?\s*$/.test(s);
  const isStatus = (s) => /⏵⏵|bypass permissions|shift\+tab to cycle|esc to interrupt/i.test(s);
  let hi = norm.length - 1;
  for (let i = norm.length - 1; i >= 0; i--) {
    if (isStatus(norm[i])) { hi = i - 1; break; }
  }
  for (let i = hi; i >= 1; i--) {
    if (!/^\s*(?:[│|]\s*)?[>❯]/.test(norm[i])) continue;
    if (!isRule(norm[i - 1])) continue;
    const strip = (s) => s.replace(/^\s*(?:[│|]\s*)?/, '').replace(/\s*[│|]\s*$/, '');
    const parts = [strip(norm[i]).replace(/^[>❯]\s?/, '')];
    for (let j = i + 1; j <= hi; j++) {
      if (isRule(norm[j])) break;
      parts.push(strip(norm[j]));
    }
    return { found: true, text: parts.join(' ').trim() };
  }
  return { found: false, text: '' };
}
`;

// TYPED vs OFFERED — the other half of the collision guard. claude renders an
// inline suggestion (its offered next prompt, and its ghost hints) as DIM cells:
// xterm gives them `.xterm-dim` / rgba(...,0.5) and parks the block cursor on the
// suggestion's FIRST character. Real keystrokes are never dim. Reading the rows as
// bare textContent threw that distinction away, so an offered suggestion read
// exactly like a half-typed thought and every send into that session came back
// `collision` — a dialog nobody was there to answer, defaulting to "don't deliver".
// Measured on a live emdash 2026-08-28: three chat turns deferred inside 40 minutes
// with the composer empty in all three, the whole line dim, cursor on its first char.
//
// Input is one descriptor per row, `{ text, spans: [{ text, dim }] }`. Dim runs are
// blanked to spaces of the SAME WIDTH rather than dropped, so column positions — and
// the box rules composerText() keys on — come through unchanged, which is what lets
// the same scan run over both the full frame and the typed-only one.
const BRIGHT_FN = String.raw`
function brightRows(rows) {
  return rows.map(r => {
    const spans = r.spans || [];
    if (!spans.length) return r.text || '';
    return spans.map(s => {
      const t = s.text || '';
      return s.dim ? ' '.repeat(t.length) : t;
    }).join('');
  });
}
`;

const command = process.argv[2];
const args = await readArgs();  // stdin: no command-line length cap (sidecar_args.mjs)
const port = args.port || 9222;

function out(o) { process.stdout.write(JSON.stringify(o)); }
function fail(msg) { out({ ok: false, error: msg }); process.exit(1); }

let browser;
try {
  browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
} catch {
  fail(`cannot connect to emdash CDP on 127.0.0.1:${port} — launch emdash with --remote-debugging-port=${port}`);
}
const page = browser.contexts()[0]?.pages()[0];
if (!page) fail('no emdash renderer page found over CDP');

// The sidebar VIRTUALIZES rows — anything scrolled out of view is not in the DOM at
// all, so a one-shot querySelector can't tell "absent" from "off-screen". Scan the
// scroller (.overflow-y-auto) top→bottom, letting rows render, until `label` appears.
// Both create ("New task for X") and open-send ("Open task X") need this; open-send
// not having it is what made live sessions look deleted and get duplicated.
const scrollBy = (val) => page.evaluate((v) => {
  const sc = [...document.querySelectorAll('.overflow-y-auto')].sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
  if (!sc) return 0;
  if (v === 'top') sc.scrollTop = 0; else sc.scrollTop += v;
  return sc.scrollTop;
}, val);

const scrollToFind = async (label) => {
  const find = () => page.evaluate((l) => {
    const btn = [...document.querySelectorAll('button')].find(x => x.getAttribute('aria-label') === l);
    if (btn) { btn.scrollIntoView({ block: 'center' }); return true; }
    return false;
  }, label);
  await scrollBy('top');
  await page.waitForTimeout(200);
  let lastTop = -1;
  for (let i = 0; i < 40; i++) {
    if (await find()) return true;
    const top = await scrollBy(280);          // step down
    await page.waitForTimeout(160);
    if (top === lastTop) break;               // reached the bottom
    lastTop = top;
  }
  return await find();
};

const clickLabel = (label) => page.evaluate((l) => {
  const btn = [...document.querySelectorAll('button')].find(x => x.getAttribute('aria-label') === l);
  if (!btn) return false; btn.click(); return true;
}, label);

// Shared REUSE lookup: find `task` in the (virtualized) sidebar, open it, and focus its
// live terminal input. Used by both open-send (which then reads/inserts text) and
// interrupt (which just needs the terminal focused so Escape lands in the right pane).
// Fails (via `fail`, which exits the process) on any step that can't be completed —
// callers never see a partial/ambiguous state.
// The sidebar labels this lookup reasons about, in DOM order. Read and clicked by
// the SAME filter, so an index from one evaluate names the same row in the next.
const sidebarLabels = () => page.evaluate(() => [...document.querySelectorAll('button')]
  .map(b => b.getAttribute('aria-label') || '')
  .filter(l => l.startsWith('New task for ') || l.startsWith('Open task ')));

// Find `task` under `project`'s section of the (virtualized) sidebar and click THAT
// row — never merely the first row with the same label. See sidebar_section.mjs for
// why (turn 22662f53: eva's message typed into ada's same-named session).
// Returns {found, clicked}. `action` is what is done to the row once found:
// 'click' opens it; 'contextmenu' raises its menu (close-task's delete).
const openInProject = async (task, project, action = 'click') => {
  if (!await scrollToFind(`New task for ${project}`)) return { found: false };
  await page.waitForTimeout(160);
  // A COLLAPSED project keeps its header but renders none of its task rows, so the
  // section reads as empty and every task in it looks deleted — close-task then
  // reported "absent" (success) for a task emdash still held, and it came straight
  // back on the next report (hal's box, 2026-10-12). The header's toggle says which
  // state it is in ("Expand <p>" collapsed, "Collapse <p>" open — read live off emdash
  // 1.2.8): open it, and leave it open, before reading the section.
  if (await clickLabel(`Expand ${project}`)) await page.waitForTimeout(400);
  let carried = false;
  let lastTop = -1;
  for (let i = 0; i < 40; i++) {
    const labels = (await sidebarLabels()).filter(isSidebarLabel);
    const pick = pickInSection(labels, project, task, carried);
    if (pick.state === 'found') {
      const clicked = await page.evaluate(({ index, want, action }) => {
        const rows = [...document.querySelectorAll('button')].filter(b => {
          const l = b.getAttribute('aria-label') || '';
          return l.startsWith('New task for ') || l.startsWith('Open task ');
        });
        const btn = rows[index];
        // Re-check the label: the list may have re-rendered between the two reads.
        if (!btn || btn.getAttribute('aria-label') !== want) return false;
        btn.scrollIntoView({ block: 'center' });
        if (action === 'contextmenu') btn.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }));
        else if (action === 'click') btn.click();
        // 'none': located only — the post-delete check must not touch the row.
        return true;
      }, { index: pick.index, want: `Open task ${task}`, action });
      return { found: true, clicked };
    }
    if (pick.state !== 'more') return { found: false };   // ended, or header lost
    carried = true;
    const top = await scrollBy(280);
    await page.waitForTimeout(160);
    if (top === lastTop) return { found: false };          // bottom of the sidebar
    lastTop = top;
  }
  return { found: false };
};

const openTask = async (task, project) => {
  if (project) {
    const { found, clicked } = await openInProject(task, project);
    if (!found) fail(`TASK_NOT_FOUND: no task "${task}" under project "${project}" in this emdash (archived, or another macOS account)`);
    if (!clicked) fail(`could not click task "${task}" (project "${project}") after locating it in the sidebar`);
  } else {
    // No project: the old first-match lookup. Only safe while the name is unique
    // across projects — every runner caller that knows the project passes it.
    const found = await scrollToFind(`Open task ${task}`);
    // TASK_NOT_FOUND is a claim about the WHOLE sidebar, only trustworthy now that we
    // scan all of it. Even so the caller cross-checks it against emdash's sqlite before
    // creating anything, and any LATER failure here means the task exists but the
    // interaction glitched — the caller must NOT create a duplicate.
    if (!found) fail(`TASK_NOT_FOUND: no task "${task}" in this emdash (archived, or another macOS account)`);
    if (!await clickLabel(`Open task ${task}`)) {
      fail(`could not click task "${task}" after locating it in the sidebar`);
    }
  }
  await page.waitForTimeout(1200);
  // Focus the ACTIVE terminal's input. xterm's real input is an off-screen
  // `.xterm-helper-textarea`, so a Playwright .click() fails its viewport check — we
  // focus it via JS (viewport-agnostic) instead, picking the visible xterm (the active
  // task's pane) when several are mounted.
  if (!await focusTerminal()) fail(`could not focus the terminal input for task "${task}"`);
};

// Focus the active terminal's input and CONFIRM focus is there. Returns false when
// it cannot be put there.
//
// The confirmation is the point. `keyboard.insertText` and `keyboard.press` go to
// whatever element holds focus at the instant they arrive, not to the terminal we
// meant. On 2026-10-07 a Slack message to ACE was typed into an existing session
// while Jonathan was using emdash; nothing reached that session's transcript, and
// the likeliest cause is a click of his landing between our focus and our insert,
// so the text went wherever he had clicked. The New Task path already checks focus
// moved before it inserts (see `create`); this is the same check for the terminal,
// and every keystroke-sending site calls it immediately before it types.
const focusTerminal = () => page.evaluate(String.raw`(() => { ${ACTIVE_TERM_FN}; return (() => {
  const term = activeTerm();
  const ta = (term && term.querySelector('.xterm-helper-textarea'))
    || document.querySelector('textarea[aria-label="Terminal input"]');
  if (!ta) return false;
  if (document.activeElement !== ta) ta.focus();
  return document.activeElement === ta;
})(); })()`);

// Re-assert focus right before a keystroke, once more after a short pause (a click
// in flight can steal it back), and refuse rather than type somewhere unknown.
// FOCUS_LOST is raised BEFORE anything is typed, so the runner may treat it as a
// send that never happened.
const ensureFocus = async (task) => {
  if (await focusTerminal()) return;
  await page.waitForTimeout(150);
  if (await focusTerminal()) return;
  fail(`FOCUS_LOST: the terminal input for task "${task}" would not keep focus ` +
       `(a human is probably using emdash) — refusing to type`);
};

// Read whatever is in the ACTIVE terminal's composer.
//
// Detection is structural (COMPOSER_FN: the ❯ row directly under a box rule,
// whole viewport, status-bar bounded) — NOT a "last N rows" scan. The previous
// window read a FRESH session as empty every time, because a fresh session
// draws its composer near the TOP of the pane (measured: row 13 of 38, the
// whole scanned window blank) — which is exactly the state every new chat
// session is in (#521).
// Returns {found, text, typed}: `text` is everything rendered in the composer,
// `typed` only the part the HUMAN put there (see BRIGHT_FN — dim cells are the
// suggestion claude is offering, not keystrokes). `found` comes from the full
// frame, since the box rules and ❯ that make a composer findable are never dim.
const readComposer = () => page.evaluate(String.raw`(() => { ${ACTIVE_TERM_FN}; ${COMPOSER_FN}; ${BRIGHT_FN}; return (() => {
  const term = activeTerm();
  if (!term) return { found: false, text: '', typed: '' };
  const descriptors = [...term.querySelectorAll('.xterm-rows > div')].map(r => ({
    text: r.textContent || '',
    spans: [...r.children].map(s => ({
      text: s.textContent || '',
      dim: (s.className || '').includes('xterm-dim'),
    })),
  }));
  const full = composerText(descriptors.map(d => d.text));
  const typed = composerText(brightRows(descriptors));
  // If the blanked frame LOSES the composer the structure itself was dim, which
  // this rule does not model — so fall back to the full text (a collision, the
  // human is asked) rather than to '' (send, and clobber whatever is there).
  // Every send site in this file fails closed; this one does too.
  return { found: full.found, text: full.text,
           typed: typed.found ? typed.text : full.text };
})(); })()`);

// Bring the active terminal's view back to its live bottom, if someone scrolled
// it up. `.xterm-rows` is only what the view shows, so a pane scrolled up to read
// a long reply shows that reply and no composer, and every send bounced
// COMPOSER_NOT_VISIBLE however often it was retried (turn 6c185edd, 2026-10-26:
// four claims over 90s read the identical mid-reply frame).
//
// Measured against emdash's xterm (2026-10-26): it scrolls only for a TRUSTED
// wheel — a WheelEvent dispatched from the page is ignored — so this goes
// through `page.mouse`. And its scrollbar slider cannot say "at the bottom": with
// a long scrollback one pixel of slider is many lines. So no guess about where
// the view is; wheeling down at the bottom is a no-op. It is guarded instead on
// what makes a wheel safe: the pane has scrollback (a slider shorter than its
// track — a full-screen app's alternate buffer has none, and there a wheel is
// turned into arrow keys) and the app has no mouse reporting on
// (`enable-mouse-events`, where the wheel would reach claude as input).
// Returns whether it wheeled.
const scrollTermToBottom = async () => {
  const at = await page.evaluate(String.raw`(() => { ${ACTIVE_TERM_FN}; return (() => {
    const term = activeTerm();
    if (!term || term.classList.contains('enable-mouse-events')) return null;
    const bar = term.querySelector('.xterm-scrollable-element .scrollbar.vertical');
    const slider = bar && bar.querySelector('.slider');
    if (!slider || !bar.clientHeight || slider.offsetHeight >= bar.clientHeight - 1) return null;
    const r = (term.querySelector('.xterm-screen') || term).getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  })(); })()`);
  if (!at) return false;
  // One wheel moves the view a bounded distance however large its delta, and a
  // long reply is many screens. So wheel until the frame stops changing — the
  // bottom — rather than a fixed count (five fell short on the incident's pane).
  const frame = () => page.evaluate(String.raw`(() => { ${ACTIVE_TERM_FN}; return (() => {
    const rows = activeTerm()?.querySelector('.xterm-rows');
    return rows ? rows.textContent : '';
  })(); })()`);
  await page.mouse.move(at.x, at.y);
  let before = await frame();
  for (let i = 0; i < SCROLL_MAX_WHEELS; i++) {
    await page.mouse.wheel(0, 20000);
    await page.waitForTimeout(60);
    const now = await frame();
    if (now === before) break;
    before = now;
  }
  return true;
};
// Far more than any real scrollback needs at one wheel's reach; a bound, not a target.
const SCROLL_MAX_WHEELS = 200;
const SCROLL_BUDGET_MS = 8000;

// A frame with no composer is UNREADABLE, not empty: mid-redraw right after the
// task click, a menu/dialog covering the input, or a clipped stale frame (all
// observed live). Re-read a few times for the transient cases; the caller decides
// what an unreadable frame means (open-send fails closed).
const readComposerSettled = async () => {
  let composer = { found: false, text: '', typed: '' };
  for (let attempt = 0; attempt < 4; attempt++) {
    composer = await readComposer();
    if (composer.found) break;
    // Not drawn where it should be: the pane may simply be scrolled up. Bounded,
    // so a scroll that cannot finish costs a few seconds, never the send.
    const scrolled = attempt === 0 && await Promise.race([
      scrollTermToBottom().catch(() => false),
      new Promise((resolve) => setTimeout(() => resolve(false), SCROLL_BUDGET_MS)),
    ]);
    if (scrolled) {
      await page.waitForTimeout(150);
      composer = await readComposer();
    }
    if (composer.found) break;
    await page.waitForTimeout(400);
  }
  return composer;
};

// Ghost/placeholder hints claude shows in an EMPTY input — treat as empty so the
// fast path still fires instead of popping a spurious collision dialog. `typed`
// (above) already blanks every dim cell, which is the general form of this rule;
// the prefix list stays as a second net for any hint claude renders undimmed.
const PLACEHOLDER = /^(Try |Ask |\/ for |\? for )/i;
const isEmpty = (s) => !s || PLACEHOLDER.test(s);

try {
  if (command === 'list') {
    const data = await page.evaluate(() => {
      const labels = [...document.querySelectorAll('button')].map(b => b.getAttribute('aria-label') || '');
      return {
        tasks: labels.filter(t => t.startsWith('Open task ')).map(t => t.slice('Open task '.length)),
        projects: labels.filter(t => t.startsWith('New task for ')).map(t => t.slice('New task for '.length)),
      };
    });
    out({ ok: true, ...data });

  } else if (command === 'create') {
    const { project, prompt, taskName } = args;
    const taskNames = () => page.evaluate(() =>
      [...document.querySelectorAll('button')].map(b => b.getAttribute('aria-label') || '')
        .filter(t => t.startsWith('Open task ')).map(t => t.slice('Open task '.length)));
    if (!await scrollToFind(`New task for ${project}`)) {
      fail(`no "New task for ${project}" control — project "${project}" not found in the emdash sidebar`);
    }
    await clickLabel(`New task for ${project}`);
    await page.waitForTimeout(1000);
    // Set a deterministic task NAME so we don't have to detect it afterward (the
    // list-diff is unreliable under sidebar virtualization). The name input is the
    // dialog's first text input (its placeholder is emdash's auto-generated name).
    let finalName = taskName || "";
    if (taskName) {
      const named = await page.evaluate((name) => {
        const dlg = document.querySelector('[role=dialog],[class*=Dialog],[class*=modal]');
        if (!dlg) return false;
        const input = [...dlg.querySelectorAll('input')].find(i => (i.type === 'text' || !i.type) && i.value !== 'claude' && i.value !== 'on');
        if (!input) return false;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(input, name);                                   // React-friendly value set
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }, taskName);
      if (!named) finalName = "";   // fell back — will diff below
    }
    const before = await taskNames();
    // Initial-conversation prompt is a contenteditable in the Create Task dialog.
    //
    // Focused via JS, NOT `locator.click()`. Playwright's click first waits for the
    // element to be visible, enabled and STABLE — and "stable" means its box was
    // unchanged across two ANIMATION FRAMES. macOS renders one login session at a
    // time, so while the display belongs to the other macOS account this window
    // paints nothing, no frames arrive, and the click hangs for its full 30s even
    // though CDP answers perfectly. This was the file's last actionability-gated
    // call and so its only occlusion-sensitive one, against the promise made at the
    // top of this file; it cost two turns and marked the box not-ready on
    // 2026-08-10, ~6 minutes after the other account logged in. Separate CDP ports
    // isolate the two runners, but the SCREEN is machine-wide and cannot be split.
    // `keyboard.insertText` dispatches to the focused element with no actionability
    // check, so it needs no equivalent.
    const CE_SELECTOR = '[role=dialog] [contenteditable="true"], [class*=Dialog] [contenteditable="true"]';
    // The wait still has to exist — the dialog is React-rendered and the 1s above is
    // not a guarantee. On 2026-07-30 this call site spent its 30s because the dialog
    // never appeared AT ALL, which must stay a legible failure rather than become an
    // instant false negative. It polls on a TIMER because Playwright's default
    // polling is 'raf', which would reintroduce the exact dependency this removes.
    try {
      await page.waitForFunction((sel) => !!document.querySelector(sel),
                                 CE_SELECTOR, { polling: 250, timeout: 30000 });
    } catch {
      fail('the New Task dialog never rendered its prompt editor');
    }
    const focusedPrompt = await page.evaluate((sel) => {
      const ce = document.querySelector(sel);
      if (!ce) return false;
      ce.focus();
      // Confirm focus actually MOVED. insertText goes wherever focus is, so a silent
      // no-op here would type the prompt into the task-name input (or nowhere) and
      // still report the task as created.
      return document.activeElement === ce || ce.contains(document.activeElement);
    }, CE_SELECTOR);
    if (!focusedPrompt) fail('could not focus the New Task dialog prompt editor');
    await page.keyboard.insertText(prompt);   // atomic commit, not char-by-char
    // The SUBMIT button, matched exactly. A loose /create/i match is what broke on
    // emdash 1.2.8 (2026-10-11): the dialog grew a "Create new worktree" select and a
    // "Create new branch" tab ahead of the submit, so the click opened a dropdown, the
    // dialog sat there filled-in, and this reported "created" for a task that never
    // existed. The submit carries aria-label="Create"; its text is "Create" plus the
    // ⌘⏎ shortcut glyphs.
    const created = await page.evaluate(() => {
      const dlg = document.querySelector('[role=dialog],[class*=Dialog],[class*=modal]');
      if (!dlg) return false;
      const btns = [...dlg.querySelectorAll('button')];
      const btn = btns.find(b => b.getAttribute('aria-label') === 'Create')
        || btns.find(b => (b.textContent || '').replace(/[^A-Za-z]/g, '') === 'Create');
      if (!btn || btn.disabled) return false;
      window.__canopyCreateDialog = dlg;
      btn.click(); return true;
    });
    if (!created) fail('could not find the Create button in the New Task dialog');
    // A click is not a task. Confirm the dialog actually went away (emdash closes it
    // only once the task exists); if it is still up, the click hit something else and
    // the human is left looking at a filled-in dialog — fail loudly instead.
    try {
      await page.waitForFunction(() => {
        const d = window.__canopyCreateDialog;
        return !d || !d.isConnected || d.hasAttribute('data-closed') || d.getClientRects().length === 0;
      }, null, { polling: 250, timeout: 15000 });
    } catch {
      fail('clicked Create but the New Task dialog is still open — the task was not created');
    }
    await page.waitForTimeout(2000);
    if (!finalName) {
      // Fallback: diff (best-effort; may be imprecise under virtualization).
      const after = await taskNames();
      const beforeSet = new Set(before);
      finalName = (after.filter(n => !beforeSet.has(n)))[0] || "";
    }
    out({ ok: true, action: 'created', task: finalName });

  } else if (command === 'open-send') {
    // REUSE: open an EXISTING task and send text into its live terminal. Scroll the
    // virtualized sidebar to reach it — a one-shot query only sees the ~visible rows,
    // so with dozens of tasks the target is usually absent from the DOM despite being
    // live (observed 2026-07-15: eva's org-research session, present in emdash's DB,
    // reported TASK_NOT_FOUND and duplicated).
    const { task, text, clearFirst, project } = args;
    await openTask(task, project);

    // Read whatever is ALREADY sitting in the composer. Non-empty means the human
    // was typing when emdash switched to this task and their keystrokes leaked in —
    // a COLLISION we must NOT clobber blindly (insertText APPENDS and Enter submits
    // the concatenation; verified live 2026-07-28, a half-typed thought and a chat
    // message reached the agent as ONE line). See readComposer for how.
    //
    // The runner treats a CDPError as "task exists but undrivable" and retries the
    // turn; it never duplicates the session.
    const composer = await readComposerSettled();
    if (!composer.found) {
      fail(`COMPOSER_NOT_VISIBLE: no input line in the rendered frame for task "${task}" ` +
           `(mid-redraw, a menu is up, or a stale frame) — refusing a blind send`);
    }

    // Type `body` and submit it. Claude Code reads a burst of input that contains
    // a newline as a PASTE, and an Enter arriving inside that same burst becomes
    // part of the paste — a newline in the composer, not a submit. insertText and
    // an immediate Enter are exactly that burst, so a multi-line message (a Slack
    // reply carrying an attachment note, a web chat with a screenshot path) sat
    // typed-but-unsent in the composer and the turn failed as undelivered
    // (turn fe0a0280, 2026-10-05). Reproduced against `claude` on a raw pty: text
    // then Enter in one write never submits; any gap of 50ms or more always does.
    // So: pause before Enter, then LOOK — if the composer still holds the message,
    // the Enter was swallowed and pressing it again cannot send it twice.
    //
    // A SINGLE-line message gets the same look, after the same pause. It used to be
    // Enter-and-return, so a swallowed Enter (a keystroke lost to a human's click,
    // or a TUI mid-redraw) left the message sitting in the composer with nobody the
    // wiser until the transcript check gave up (2026-10-07, ACE). There the match is
    // by TEXT rather than emptiness: a short one-liner is never collapsed, and a slash
    // command's first Enter can legitimately REPLACE the text with an autocomplete
    // pick — pressing again then would send something we never typed.
    //
    // A LONG one-liner is collapsed too, so it also waits before Enter. It reaches
    // claude in more than one chunk: the first is read as a paste and collapsed,
    // the rest lands as typed text, and the composer shows "[Pasted text #1] …the
    // end of the message". The Enter was swallowed and the recheck, matching only
    // the whole text or a lone placeholder, never pressed it again: ACE's chat
    // follow-up sat unsent, twice (turn 50bfdf45, 2026-10-14, investigation 2).
    const PASTE_SETTLE_MS = 500;
    const PASTED = /^\[Pasted text #\d+[^\]]*\]$/;
    const PASTED_LEAD = /^(?:\[Pasted text #\d+[^\]]*\]\s*)+/;
    const stillOurs = (body, typed) => {
      if (isEmpty(typed)) return false;
      if (/\n/.test(body)) return true;           // only ever our paste (see below)
      if (squash(typed) === squash(body) || PASTED.test(typed.trim())) return true;
      const lead = typed.trim().match(PASTED_LEAD);
      const tail = lead ? squash(typed.trim().slice(lead[0].length)) : '';
      return !!tail && squash(body).includes(tail);
    };
    // Take our message back off the END of the line, leaving the human's draft as
    // they left it. Deletes only while the line still ends in our words, re-reading
    // between batches, so it stops at their text rather than counting into it.
    // False means it could not (a collapsed paste, or the line changed under us).
    const takeBackOurs = async (body, typed) => {
      const want = draftWithoutOurs(body, typed);
      if (want === null) return false;
      for (let round = 0; round < 10; round++) {
        const cur = await readComposer();
        if (!cur.found) return false;
        const line = squash(cur.typed);
        if (line === want) return true;
        if (line.length < want.length || !line.startsWith(want)) return false;
        await ensureFocus(task);
        // Squashed length counts no spaces, so one batch never overshoots.
        const extra = [...line.slice(want.length)].length;
        for (let k = 0; k < extra; k++) await page.keyboard.press('Backspace');
      }
      return false;
    };

    // Returns null once sent, or {line, when, restored} when a human's text turned
    // out to be in the composer after all — the caller hands that back as a
    // `collision`, so the human is asked (the "Clear & send" dialog) and nothing
    // of theirs is submitted. `guard` is off only for "Clear & send", where the
    // human already chose to discard the line.
    const submit = async (body, guard) => {
      await ensureFocus(task);
      if (guard) {
        // The first read can see an empty composer that is not (turn 64cada76,
        // 2026-10-23: Jonathan's draft, missed). Look again at the last moment.
        const now = await readComposer();
        if (now.found && !isEmpty(now.typed)) return { line: now.typed, when: 'before-type' };
      }
      // A lone "?", "!" or "/" is a key binding in an empty prompt, not text — see
      // typed_text.mjs. The recheck below squashes whitespace, so it still matches.
      await page.keyboard.insertText(asTyped(body));   // atomic commit, not char-by-char
      await page.waitForTimeout(PASTE_SETTLE_MS);
      if (guard) {
        // And once more after typing, BEFORE Enter: a draft both reads missed now
        // sits in front of our message, and Enter would send the two as one line.
        // See composer_check.mjs.
        const typed = await readComposer();
        if (typed.found && hasForeignLead(body, typed.typed)) {
          const restored = await takeBackOurs(body, typed.typed);
          const after = await readComposer();
          return { line: after.found ? after.typed : typed.typed, when: 'after-type', restored };
        }
      }
      await page.keyboard.press('Enter');
      // Only ever called on a composer that was empty (or just cleared), so
      // anything typed in it now is this message — for a multi-line body matched by
      // emptiness, not by text, because claude collapses a long paste to
      // "[Pasted text #1 +N lines]".
      for (let attempt = 0; attempt < 2; attempt++) {
        await page.waitForTimeout(PASTE_SETTLE_MS);
        const after = await readComposer();
        if (!after.found || !stillOurs(body, after.typed)) return;
        await ensureFocus(task);
        await page.keyboard.press('Enter');
      }
      return null;
    };

    if (clearFirst) {
      // The human chose "Clear & send": deterministically empty the input, then send.
      // Ctrl+U (kill-to-start) as a fast path, then backspace the MEASURED content and
      // re-read until empty (self-correcting; robust to whatever line editing claude's
      // TUI actually honors). Leaked text is "the last few words", so this is short.
      await ensureFocus(task);
      await page.keyboard.press('Control+U');
      for (let i = 0; i < 6; i++) {
        const cur = await readComposer();
        // `typed`, not `text`: once Ctrl+U empties the line claude re-offers a
        // suggestion into it, and measuring THAT would backspace forever against
        // text no keystroke can delete.
        const n = Math.min(cur.found ? cur.typed.length : 0, 300);
        if (n === 0) break;
        await page.keyboard.press('End');
        for (let k = 0; k < n; k++) await page.keyboard.press('Backspace');
      }
      await submit(text, false);
      out({ ok: true, action: 'sent-cleared', task });
    } else if (!isEmpty(composer.typed)) {
      // Don't touch it — hand the collision back to the runner to ask the human.
      out({ ok: true, action: 'collision', task, line: composer.typed });
    } else {
      const late = await submit(text, true);
      // `pre` is what the first read saw, so a late collision says WHICH read missed.
      const pre = { found: composer.found, typed: composer.typed };
      if (late) out({ ok: true, action: 'collision', task, line: late.line,
                      when: late.when, restored: late.restored, pre });
      else out({ ok: true, action: 'sent', task, pre });
    }

  } else if (command === 'interrupt') {
    // Cancel: open the task exactly as open-send does (find + focus its terminal),
    // but instead of inserting text just press Escape — Claude Code's TUI treats Esc
    // as "stop the current turn" mid-flight.
    //
    // AND THEN CHECK. This used to be `press('Escape'); out({ok:true})` — a keypress
    // fired at a pane and reported as a stop, with no readback of any kind. Every
    // send site in this file verifies what it did; the one command whose entire job
    // is "make the agent stop" did not, so a stop that missed was indistinguishable
    // from one that landed, all the way up to a web UI that said "cancelled".
    //
    // The running-state signal is Claude Code's own status line, `esc to interrupt`
    // — the same marker `activeTerm`, `composerText` and the send-keys pane check
    // already key off, so this adds no new contract to drift (verify-emdash covers
    // it). Two presses, because Esc is context-sensitive: with a dialog up the first
    // one dismisses the dialog and the turn keeps running.
    //
    // Reports FACTS, never a verdict, and never fails the command — the runner
    // decides what a non-stop means (chat_pump.finish_chat_bridge):
    //   interrupted  = was running, is not now
    //   idle         = nothing was running to interrupt (a stop that raced the reply)
    //   still-running = pressed twice, the status line is still there
    //   unreadable   = could not see the frame; we make NO claim either way
    const { task, project } = args;
    await openTask(task, project);
    const running = () => page.evaluate(String.raw`(() => { ${ACTIVE_TERM_FN}; return (() => {
      const term = activeTerm();
      const rows = term && term.querySelector('.xterm-rows');
      if (!rows) return null;                       // no frame to read — not "stopped"
      const text = rows.textContent || '';
      if (/esc to interrupt/i.test(text)) return true;
      // A frame we can positively identify as Claude's, without the running marker,
      // is a real "not running". Anything else is unreadable, not idle — the
      // distinction this whole branch exists to preserve.
      if (/[⏺✻⎿]/.test(text) || /shift\\+tab to cycle|bypass permissions/i.test(text)) return false;
      return null;
    })(); })()`);

    const before = await running();
    if (before === false) { out({ ok: true, task, action: 'idle' }); }
    else {
      let state = before;
      for (let attempt = 0; attempt < 2; attempt++) {
        await page.keyboard.press('Escape');   // Claude Code: Esc interrupts the running turn
        // Give the TUI time to repaint its status line before believing the frame.
        for (let i = 0; i < 5; i++) {
          await page.waitForTimeout(400);
          state = await running();
          if (state === false) break;
        }
        if (state === false) break;
      }
      out({ ok: true, task,
            action: state === false ? 'interrupted' : state === true ? 'still-running' : 'unreadable' });
    }

  } else if (command === 'close-task') {
    // Delete `task` from the sidebar (the designed close behaviour). emdash's context
    // menu also offers Archive — a gentler alternative, unexplored here — but delete
    // is what this design chose, and it is verifiable before reporting.
    //
    // ABSENT IS SUCCESS, not TASK_NOT_FOUND. Unlike open-send, where absence means
    // "we must not create a duplicate", here the caller wants the task gone and it
    // already is — a double-tap from the phone and a human who just deleted it in
    // emdash both land here.
    //
    // Found by probe-close.mjs against a live emdash 1.1.40, 2026-07-31: the row's
    // "Open task {task}" button offers no visible per-row control on hover — the
    // delete affordance is a right-click CONTEXT MENU (contextmenu on that same
    // button) with items "Pin task" / "Rename" / "Archive" / "Copy branch name" /
    // "Delete" (rendered as <div class="group/context-menu-item ...">, no
    // role=menuitem). "Delete" opens a confirmation dialog (role=dialog, title
    // "Delete task", body: `"<task>" will be permanently deleted. This action
    // cannot be undone.`) with two buttons: "Cancel" and "Delete⌘⏎" — clicking the
    // latter deletes it immediately (verified live: task vanished from the sidebar).
    // With `project`, the row is found AND acted on inside that project's sidebar
    // section — two agents can each own a task with this name, and a delete aimed
    // by label alone removes whichever is listed first (sidebar_section.mjs). The
    // runner always passes it (close.close_session refuses rather than omit it).
    const { task, project } = args;
    let found, opened;
    if (project) {
      ({ found, clicked: opened } = await openInProject(task, project, 'contextmenu'));
    } else {
      found = await scrollToFind(`Open task ${task}`);
      opened = found && await page.evaluate((t) => {
        const btn = [...document.querySelectorAll('button')]
          .find(x => x.getAttribute('aria-label') === `Open task ${t}`);
        if (!btn) return false;
        btn.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }));
        return true;
      }, task);
    }
    if (!found) { out({ ok: true, action: 'absent' }); }
    else {
      if (!opened) fail(`could not reach the controls for task "${task}"`);
      await page.waitForTimeout(400);

      const clicked = await page.evaluate(() => {
        const item = [...document.querySelectorAll('[class*="context-menu-item"], [role=menuitem]')]
          .find(e => /^\s*delete\s*$/i.test(e.textContent || ''));
        if (!item) return false; item.click(); return true;
      });
      if (!clicked) fail(`no delete control for task "${task}" — emdash's UI may have changed; re-run probe-close.mjs`);
      await page.waitForTimeout(400);

      // Confirmation dialog ("Delete task" / "Cancel" / "Delete⌘⏎"). Confirm it.
      const confirmed = await page.evaluate(() => {
        const dlg = document.querySelector('[role=dialog]');
        if (!dlg) return false;
        const yes = [...dlg.querySelectorAll('button')]
          .find(b => /delete/i.test(b.textContent || '') && !/cancel/i.test(b.textContent || ''));
        if (!yes) return false; yes.click(); return true;
      });
      if (!confirmed) fail(`no confirmation dialog for deleting task "${task}" — emdash's UI may have changed; re-run probe-close.mjs`);
      await page.waitForTimeout(900);

      // VERIFY. The whole design rests on this: the server wrote nothing, so a
      // close we merely attempted must not be reported as done.
      const gone = project
        ? !(await openInProject(task, project, 'none')).found
        : !(await scrollToFind(`Open task ${task}`));
      if (!gone) fail(`task "${task}" is still in the sidebar after the delete`);
      out({ ok: true, action: 'deleted' });
    }

  } else if (command === 'add-project') {
    // Make the local git checkout at `path` the emdash project `name`, through
    // emdash's own Add Project dialog ("Pick Directory" mode) — emdash validates
    // the directory is a git repo and registers it; we never write its DB. The
    // runner clones the repo itself first (its own git credentials) and checks
    // emdash's DB for the row afterwards: a dialog that closed is not a project.
    //
    // The dialog's Directory field is a BUTTON that opens the native folder
    // picker (`openSelectDirectoryDialog`), which CDP cannot drive. Its React
    // props carry the field's `onPathChange` — the very callback the picker
    // calls with the chosen folder — so the path is handed to it directly,
    // reached through the button's React fiber. Everything after that is
    // emdash's normal flow: its path inspection enables Create, and Create runs
    // its own createProject. Read out of emdash 1.2.8's bundle (DirectoryField,
    // AddProjectModal) and checked live 2026-10-24: the path lands in the field,
    // the name placeholder becomes the folder's basename, Create enables.
    //
    // Output: {ok, action:"submitted", name, path}. Never leaves the dialog up:
    // any failure after it opened closes it, so a human is not left looking at
    // a half-filled Add Project.
    const { name, path } = args;
    if (!name || !path) fail('add-project needs {name, path}');
    const openDialog = () => page.evaluate(() => !!document.querySelector('[role=dialog]'));
    if (await openDialog()) {
      fail('BUSY: a dialog is already open in emdash (a person is probably using it) — not adding a project now');
    }
    if (!await clickLabel('Add Project')) {
      fail('no "Add Project" control in the emdash sidebar — emdash\'s UI may have changed');
    }
    const closeDialog = () => page.evaluate(() => {
      const dlg = document.querySelector('[role=dialog]');
      if (!dlg) return;
      const close = [...dlg.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === 'Close');
      if (close) close.click();
      else document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    const bail = async (msg) => { await closeDialog(); fail(msg); };
    try {
      await page.waitForFunction(() => {
        const d = document.querySelector('[role=dialog]');
        return !!d && /Add Project/.test(d.textContent || '');
      }, null, { polling: 250, timeout: 10000 });
    } catch {
      await bail('the Add Project dialog never rendered');
    }
    // "Pick Directory" is the dialog's default mode from this button; click it
    // anyway so a remembered other mode cannot route the path elsewhere.
    await page.evaluate(() => {
      const dlg = document.querySelector('[role=dialog]');
      const card = [...dlg.querySelectorAll('button,[role=button],[class*=card]')]
        .find(e => (e.textContent || '').trim() === 'Pick Directory');
      if (card) card.click();
    });
    await page.waitForTimeout(300);
    const injected = await page.evaluate((p) => {
      const dlg = document.querySelector('[role=dialog]');
      if (!dlg) return 'no-dialog';
      const propOf = (el, prop) => {
        const key = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
        let f = key ? el[key] : null;
        for (let i = 0; f && i < 40; i++, f = f.return) {
          const mp = f.memoizedProps;
          if (mp && typeof mp[prop] === 'function') return mp[prop];
        }
        return null;
      };
      for (const btn of dlg.querySelectorAll('button')) {
        const onPathChange = propOf(btn, 'onPathChange');
        if (onPathChange) { onPathChange(p); return 'ok'; }
      }
      return 'no-field';
    }, path);
    if (injected !== 'ok') {
      await bail(`could not set the Add Project directory (${injected}) — emdash's dialog may have changed`);
    }
    await page.waitForTimeout(400);
    // The project NAME is the dialog's bare text input (placeholder: the
    // folder's basename). Set it explicitly: an agent's project must be named
    // exactly its slug, whatever the folder is called.
    const named = await page.evaluate((n) => {
      const dlg = document.querySelector('[role=dialog]');
      const input = dlg && [...dlg.querySelectorAll('input')].find(i => (i.type === 'text' || !i.type));
      if (!input) return false;
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(input, n);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return input.value === n;
    }, name);
    if (!named) await bail('could not set the project name in the Add Project dialog');
    // Create enables once emdash has inspected the path (a git repo, not already
    // registered). Its own error text, when it shows one, is the reason.
    // The submit is the dialog button whose letters read exactly "Create" (its
    // text is "Create⌘⏎"), the same exact match `create` uses for New Task.
    try {
      await page.waitForFunction(() => {
        const dlg = document.querySelector('[role=dialog]');
        const b = dlg && [...dlg.querySelectorAll('button')]
          .find(x => (x.textContent || '').replace(/[^A-Za-z]/g, '') === 'Create');
        return !!b && !b.disabled;
      }, null, { polling: 250, timeout: 15000 });
    } catch {
      const said = await page.evaluate(() => {
        const dlg = document.querySelector('[role=dialog]');
        return dlg ? (dlg.textContent || '').replace(/\s+/g, ' ').slice(0, 300) : '';
      });
      await bail(`emdash would not accept ${path} as a project (Create stayed disabled): ${said}`);
    }
    await page.evaluate(() => {
      const dlg = document.querySelector('[role=dialog]');
      const b = dlg && [...dlg.querySelectorAll('button')]
        .find(x => (x.textContent || '').replace(/[^A-Za-z]/g, '') === 'Create');
      if (b) { window.__canopyAddProjectDialog = dlg; b.click(); }
    });
    try {
      await page.waitForFunction(() => {
        const d = window.__canopyAddProjectDialog;
        return !d || !d.isConnected || d.hasAttribute('data-closed') || d.getClientRects().length === 0;
      }, null, { polling: 250, timeout: 20000 });
    } catch {
      await bail('clicked Create but the Add Project dialog is still open — the project was not added');
    }
    out({ ok: true, action: 'submitted', name, path });

  } else if (command === 'composer') {
    // READ-ONLY: what is in this task's composer, and what is on its screen. The
    // runner asks after a send it could not confirm in the transcript, to decide
    // between pressing Enter on our own unsent message, believing a submit the
    // transcript has not shown yet, retyping into an empty composer, or leaving a
    // human's text alone (execute._recover_delivery). Types nothing; opening the
    // task is no new hazard — open-send opened this same task seconds earlier.
    //   found    composer visible (false = unreadable frame; the runner fails closed)
    //   typed    the non-dim composer text (what a human — or we — put there)
    //   screen   the whole rendered frame, for "is our message already up there?"
    //   running  claude's own "esc to interrupt" marker is showing
    const { task, project } = args;
    await openTask(task, project);
    const composer = await readComposerSettled();
    const screen = await page.evaluate(String.raw`(() => { ${ACTIVE_TERM_FN}; return (() => {
      const term = activeTerm();
      const rows = term && term.querySelector('.xterm-rows');
      if (!rows) return '';
      return [...rows.children].map(r => r.textContent).join('\n');
    })(); })()`);
    out({ ok: true, task, found: composer.found, text: composer.text, typed: composer.typed,
          screen: screen || '', running: /esc to interrupt/i.test(screen || '') });

  } else if (command === 'read-term') {
    // The rendered terminal, as TEXT. This is how canopy sees a dialog that only
    // exists on screen: a hook can say an agent is blocked but never what it is
    // asking, and emdash (not canopy) owns the session, so the menu lives here.
    //
    // Read the DOM rather than the PTY on purpose — emdash's xterm uses the DOM
    // renderer, so it has already resolved the TUI's cursor-movement escapes into
    // real cells. Parsing the raw stream instead welds words together, because
    // Claude Code draws spaces as ESC[nC.
    const { task, project } = args;
    await openTask(task, project);
    const text = await page.evaluate(String.raw`(() => { ${ACTIVE_TERM_FN}; return (() => {
      const term = activeTerm();
      const rows = term && term.querySelector('.xterm-rows');
      if (!rows) return null;
      return [...rows.children].map(r => r.textContent).join('\n');
    })(); })()`);
    if (text === null) fail(`could not read the terminal for task "${task}"`);
    out({ ok: true, task, text });

  } else if (command === 'send-keys') {
    // Answer a dialog. Keys are sent one at a time so a menu answer is exactly
    // "3" then Enter, never a pasted string — insertText would put the digit in
    // the prompt of a session that is NOT showing a menu.
    const { task, keys, project } = args;
    await openTask(task, project);
    // A task can hold several terminals — a Claude pane plus shell tabs the human
    // opened — and only the SELECTED one is rendered at full size. With a shell
    // tab selected, every answer to a real dialog died here (labs, 2026-08-01:
    // 45 minutes of taps refused, correctly and invisibly). The human asked for
    // this keystroke, so selecting the Claude tab for them is the on-demand case
    // that is allowed to move emdash's UI — unlike a read on a signal, which is
    // what #510 was reverted for.
    //
    // Two rules make this safe to do blind:
    //   * The tab is matched by `title` starting "Claude (" — never by position
    //     and never by an aria-label starting "Close", which is the button that
    //     KILLS the session and sits inside this very tab.
    //   * It is dispatched as a direct `.click()` rather than a real mouse click,
    //     so no overlay can intercept it by sitting on top of the coordinates.
    // The positive identification below still has the final say either way: if
    // this fails, or selects something that is not a Claude session, we refuse
    // exactly as before.
    const switched = await page.evaluate(String.raw`(() => {
      const visible = (el) => {
        if (!el || !el.offsetParent) return false;      // other tasks' stale DOM
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };
      const tabs = [...document.querySelectorAll('[role="button"][title]')]
        .filter((el) => /^Claude\s*\(/.test(el.getAttribute('title') || ''))
        .filter(visible);
      if (tabs.length !== 1) return false;   // ambiguous is not actionable
      tabs[0].click();
      return true;
    })()`);
    if (switched) await page.waitForTimeout(400);
    // Refuse if the pane in view is not a Claude session. emdash renders only the
    // ACTIVE terminal at full size, so if the human has a SHELL tab selected in
    // this task, that shell is what "largest" resolves to — and pressing "3" then
    // Enter there RUNS something. Reading the wrong pane costs a menu; typing
    // into it is the one outcome worth failing for, so this command alone
    // requires a positive identification instead of falling back.
    const isClaude = await page.evaluate(String.raw`(() => { ${ACTIVE_TERM_FN}; return (() => {
      const term = activeTerm();
      const rows = term && term.querySelector('.xterm-rows');
      const text = rows ? rows.textContent || '' : '';
      // Claude's own chrome: transcript glyphs, or the composer's status line.
      if (/[⏺✻⎿]/.test(text)) return true;
      if (/esc to interrupt|shift\\+tab to cycle|bypass permissions/i.test(text)) return true;
      // A DIALOG's footer. Needed because both tests above look for chrome that a
      // tall dialog PUSHES OFF THE FRAME: Claude Code draws a dialog where the
      // composer would be, so a fresh session whose first act is a six-option
      // AskUserQuestion shows the dialog and nothing else — no glyphs, no status
      // line. The pane was then unidentifiable exactly when a menu was up, which
      // is the only time this command is ever called, and every web answer to one
      // was refused as NOT_A_CLAUDE_PANE (observed live 2026-08-12). These
      // footers are drawn by Claude Code alone; a shell renders none of them.
      if (/Enter to select|Enter to confirm|Tab to amend|ctrl\\+e to explain|to navigate · Esc to cancel/i.test(text)) return true;
      // The dialog's STRUCTURE, for the states that draw no footer at all.
      // The review tab is one: it shows the tab strip, the answers so far and
      // "1. Submit answers", and nothing else — so footer matching alone still
      // could not identify the pane at the exact moment the final Submit had to
      // be pressed, leaving a fully-filled-in dialog stranded one keypress from
      // done (observed live 2026-08-12). Ballot-box glyphs and this phrasing are
      // Claude Code's; a shell draws neither.
      return /[☐☒]|✔ Submit|Ready to submit your answers/.test(text);
    })(); })()`);
    if (!isClaude) {
      fail(`NOT_A_CLAUDE_PANE: the visible terminal for "${task}" is not a Claude session ` +
           `(a shell tab is probably selected${switched ? ", and selecting the Claude tab did not help" : ""}) ` +
           `— refusing to send keys into it`);
    }
    // Playwright NAMES its keys; a raw control character is rejected outright
    // ("Unknown key: \t"). Every control character the answer path can emit has
    // to be mapped here or the sequence dies part-way — having already pressed
    // the keys before it, which on a multi-select leaves checkboxes toggled and
    // nothing submitted. Tab was missing, so answering a TABBED dialog failed
    // right after its first tab (observed live 2026-08-12). It survived a PTY
    // harness because that writes the raw byte and a terminal interprets it;
    // this transport does not.
    const NAMED_KEYS = { '\r': 'Enter', '\n': 'Enter', '\u001b': 'Escape', '\t': 'Tab',
                         '\u001b[A': 'ArrowUp', '\u001b[B': 'ArrowDown',
                         '\u001b[C': 'ArrowRight', '\u001b[D': 'ArrowLeft' };
    // "text:..." means TYPE this, not press it — the answer to a question whose
    // real answer is not on the menu ("Type something"). keyboard.press takes one
    // key and would reject a sentence outright, and pressing it character by
    // character gets the shifting wrong on punctuation; keyboard.type is the
    // primitive for a string.
    const TEXT_PREFIX = 'text:';
    for (const key of keys) {
      if (typeof key === 'string' && key.startsWith(TEXT_PREFIX)) {
        // `delay` matters: at zero the characters arrive faster than the TUI
        // processes them and the row takes the text WITHOUT ticking itself,
        // after which Enter neither commits nor advances and the dialog is
        // stuck. Discrete, paced keys are what a person produces and what the
        // row's own handler is written for (matches a PTY capture exactly).
        await page.keyboard.type(key.slice(TEXT_PREFIX.length), { delay: 60 });
        // Longer than a keypress needs. The TUI re-renders the row it just took
        // text into, and the Enter that COMMITS that text has to arrive after
        // that settles — at 120ms it landed mid-render and toggled the row off
        // instead, leaving the answer typed but unticked and the dialog stuck on
        // the same tab (observed live 2026-08-13).
        await page.waitForTimeout(700);
      } else {
        await page.keyboard.press(NAMED_KEYS[key] || key);
        await page.waitForTimeout(120);
      }
    }
    out({ ok: true, task, sent: keys.length });

  } else if (command === 'probe') {
    // READ-ONLY upgrade probe: does every DOM contract this sidecar depends on still
    // resolve? Clicks NOTHING and opens NOTHING — an upgrade check that stole focus
    // or selected a tab would be a check nobody dares run on a working fleet, and
    // #510 is the record of what a read-on-a-signal costs.
    //
    // It reports what it can SEE, so a contract is only meaningful when the UI is
    // showing that part of itself: with no task open there is no terminal, which is
    // not a drift. Hence `present` (found it), `absent` (looked, not there) and the
    // caller deciding which absences matter — see upgrade_check.CDP_CONTRACTS.
    const data = await page.evaluate(String.raw`(() => { ${ACTIVE_TERM_FN}; return (() => {
      const labels = [...document.querySelectorAll('button')]
        .map(b => b.getAttribute('aria-label') || '');
      const term = activeTerm();
      const claudeTabs = [...document.querySelectorAll('[role="button"][title]')]
        .filter(el => /^Claude\s*\(/.test(el.getAttribute('title') || ''));
      return {
        open_task_labels:  labels.filter(t => t.startsWith('Open task ')).length,
        new_task_labels:   labels.filter(t => t.startsWith('New task for ')).length,
        sidebar_scroller:  document.querySelectorAll('.overflow-y-auto').length,
        xterm_any:         document.querySelectorAll('.xterm').length,
        xterm_active:      term ? 1 : 0,
        xterm_rows:        term && term.querySelector('.xterm-rows') ? 1 : 0,
        terminal_input:    (term && term.querySelector('.xterm-helper-textarea'))
                             || document.querySelector('textarea[aria-label="Terminal input"]') ? 1 : 0,
        claude_tabs:       claudeTabs.length,
        add_project_button: labels.filter(t => t === 'Add Project').length,
      };
    })(); })()`);
    out({ ok: true, ...data });

  } else {
    fail(`unknown command: ${command}`);
  }
} catch (e) {
  fail(String((e && e.message) || e));
} finally {
  await browser.close();
}
