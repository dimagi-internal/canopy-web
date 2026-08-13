// emdash CDP control sidecar — drives the RUNNING emdash app over the Chrome
// DevTools Protocol (emdash is Electron; launch it with --remote-debugging-port).
// This is the sanctioned path (no DB injection, no app patching): tasks created
// here flow through emdash's own UI, so they appear live in the sidebar and run
// interactive `claude` on the subscription. Commands (arg = JSON on argv[3]):
//   list                              -> {ok, tasks:[names], projects:[names]}
//   create {project, prompt}          -> {ok, action:"created"}    (new session)
//   open-send {task, text}            -> {ok, action:"sent", task} (REUSE existing)
//                                        -> {ok, action:"collision", line} if the prompt
//                                           already holds UNSENT text (human was typing when
//                                           emdash switched tasks) — does NOT clobber it.
//   open-send {task, text, clearFirst}-> {ok, action:"sent-cleared"} kills the current input
//                                           line first, then sends (the human's "Clear & send").
//   interrupt {task}                  -> {ok, task} opens the task (same lookup as open-send)
//                                           and presses Escape — Claude Code's TUI treats
//                                           this as "stop the running turn" (see runner.cancel).
//   close-task {task}                 -> {ok, action:"deleted"|"absent"} DELETES the task
//                                        from emdash (delete is the designed close behaviour).
//                                        Verifies it is gone before reporting success; "absent"
//                                        means it already was.
// Text is delivered via CDP Input.insertText (one atomic commit, not char-by-char
// typing) so it lands fast and narrows the window for a keystroke collision.
// All output is a single JSON line on stdout. Occlusion-proof: uses JS-dispatched
// clicks so it works while emdash is backgrounded (no foreground focus needed).
import { chromium } from 'playwright-core';


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

const command = process.argv[2];
const args = JSON.parse(process.argv[3] || '{}');
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
const scrollToFind = async (label) => {
  const scrollBy = (val) => page.evaluate((v) => {
    const sc = [...document.querySelectorAll('.overflow-y-auto')].sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
    if (!sc) return 0;
    if (v === 'top') sc.scrollTop = 0; else sc.scrollTop += v;
    return sc.scrollTop;
  }, val);
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
const openTask = async (task) => {
  const found = await scrollToFind(`Open task ${task}`);
  // TASK_NOT_FOUND is a claim about the WHOLE sidebar, only trustworthy now that we
  // scan all of it. Even so the caller cross-checks it against emdash's sqlite before
  // creating anything, and any LATER failure here means the task exists but the
  // interaction glitched — the caller must NOT create a duplicate.
  if (!found) fail(`TASK_NOT_FOUND: no task "${task}" in this emdash (archived, or another macOS account)`);
  if (!await clickLabel(`Open task ${task}`)) {
    fail(`could not click task "${task}" after locating it in the sidebar`);
  }
  await page.waitForTimeout(1200);
  // Focus the ACTIVE terminal's input. xterm's real input is an off-screen
  // `.xterm-helper-textarea`, so a Playwright .click() fails its viewport check — we
  // focus it via JS (viewport-agnostic) instead, picking the visible xterm (the active
  // task's pane) when several are mounted.
  const focused = await page.evaluate(String.raw`(() => { ${ACTIVE_TERM_FN}; return (() => {
    const term = activeTerm();
    const ta = (term && term.querySelector('.xterm-helper-textarea'))
      || document.querySelector('textarea[aria-label="Terminal input"]');
    if (!ta) return false;
    ta.focus();
    return true;
  })(); })()`);
  if (!focused) fail(`could not focus the terminal input for task "${task}"`);
};

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
    const created = await page.evaluate(() => {
      const dlg = document.querySelector('[role=dialog],[class*=Dialog],[class*=modal]');
      if (!dlg) return false;
      const btn = [...dlg.querySelectorAll('button')].find(b => /create/i.test(b.textContent || '') && !/close|cancel/i.test(b.textContent || ''));
      if (!btn) return false; btn.click(); return true;
    });
    if (!created) fail('could not find the Create button in the New Task dialog');
    await page.waitForTimeout(3000);
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
    const { task, text, clearFirst } = args;
    await openTask(task);

    // Read whatever is ALREADY sitting in the composer. Non-empty means the human
    // was typing when emdash switched to this task and their keystrokes leaked in —
    // a COLLISION we must NOT clobber blindly (insertText APPENDS and Enter submits
    // the concatenation; verified live 2026-07-28, a half-typed thought and a chat
    // message reached the agent as ONE line).
    //
    // Detection is structural (COMPOSER_FN: the ❯ row directly under a box rule,
    // whole viewport, status-bar bounded) — NOT a "last N rows" scan. The previous
    // window read a FRESH session as empty every time, because a fresh session
    // draws its composer near the TOP of the pane (measured: row 13 of 38, the
    // whole scanned window blank) — which is exactly the state every new chat
    // session is in (#521).
    const readComposer = () => page.evaluate(String.raw`(() => { ${ACTIVE_TERM_FN}; ${COMPOSER_FN}; return (() => {
      const term = activeTerm();
      if (!term) return { found: false, text: '' };
      const rows = [...term.querySelectorAll('.xterm-rows > div')].map(r => r.textContent || '');
      return composerText(rows);
    })(); })()`);
    // A frame with no composer is UNREADABLE, not empty: mid-redraw right after the
    // task click, a menu/dialog covering the input, or a clipped stale frame (all
    // observed live). Re-read a few times for the transient cases, then fail closed —
    // a blind send would append into a composer we cannot see. The runner treats a
    // CDPError as "task exists but undrivable" and retries the turn; it never
    // duplicates the session.
    let composer = { found: false, text: '' };
    for (let attempt = 0; attempt < 4; attempt++) {
      composer = await readComposer();
      if (composer.found) break;
      await page.waitForTimeout(400);
    }
    if (!composer.found) {
      fail(`COMPOSER_NOT_VISIBLE: no input line in the rendered frame for task "${task}" ` +
           `(mid-redraw, a menu is up, or a stale frame) — refusing a blind send`);
    }
    // Ghost/placeholder hints claude shows in an EMPTY input — treat as empty so the
    // fast path still fires instead of popping a spurious collision dialog.
    const PLACEHOLDER = /^(Try |Ask |\/ for |\? for )/i;
    const isEmpty = (s) => !s || PLACEHOLDER.test(s);

    if (clearFirst) {
      // The human chose "Clear & send": deterministically empty the input, then send.
      // Ctrl+U (kill-to-start) as a fast path, then backspace the MEASURED content and
      // re-read until empty (self-correcting; robust to whatever line editing claude's
      // TUI actually honors). Leaked text is "the last few words", so this is short.
      await page.keyboard.press('Control+U');
      for (let i = 0; i < 6; i++) {
        const cur = await readComposer();
        const n = Math.min(cur.found ? cur.text.length : 0, 300);
        if (n === 0) break;
        await page.keyboard.press('End');
        for (let k = 0; k < n; k++) await page.keyboard.press('Backspace');
      }
      await page.keyboard.insertText(text);
      await page.keyboard.press('Enter');
      out({ ok: true, action: 'sent-cleared', task });
    } else if (!isEmpty(composer.text)) {
      // Don't touch it — hand the collision back to the runner to ask the human.
      out({ ok: true, action: 'collision', task, line: composer.text });
    } else {
      await page.keyboard.insertText(text);   // atomic commit, not char-by-char
      await page.keyboard.press('Enter');
      out({ ok: true, action: 'sent', task });
    }

  } else if (command === 'interrupt') {
    // Cancel: open the task exactly as open-send does (find + focus its terminal),
    // but instead of inserting text just press Escape — Claude Code's TUI treats Esc
    // as "stop the current turn" mid-flight.
    const { task } = args;
    await openTask(task);
    await page.keyboard.press('Escape');   // Claude Code: Esc interrupts the running turn
    out({ ok: true, task });

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
    const { task } = args;
    const found = await scrollToFind(`Open task ${task}`);
    if (!found) { out({ ok: true, action: 'absent' }); }
    else {
      const opened = await page.evaluate((t) => {
        const btn = [...document.querySelectorAll('button')]
          .find(x => x.getAttribute('aria-label') === `Open task ${t}`);
        if (!btn) return false;
        btn.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }));
        return true;
      }, task);
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
      const gone = !(await scrollToFind(`Open task ${task}`));
      if (!gone) fail(`task "${task}" is still in the sidebar after the delete`);
      out({ ok: true, action: 'deleted' });
    }

  } else if (command === 'read-term') {
    // The rendered terminal, as TEXT. This is how canopy sees a dialog that only
    // exists on screen: a hook can say an agent is blocked but never what it is
    // asking, and emdash (not canopy) owns the session, so the menu lives here.
    //
    // Read the DOM rather than the PTY on purpose — emdash's xterm uses the DOM
    // renderer, so it has already resolved the TUI's cursor-movement escapes into
    // real cells. Parsing the raw stream instead welds words together, because
    // Claude Code draws spaces as ESC[nC.
    const { task } = args;
    await openTask(task);
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
    const { task, keys } = args;
    await openTask(task);
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
    const NAMED_KEYS = { '\r': 'Enter', '\n': 'Enter', '\u001b': 'Escape', '\t': 'Tab' };
    // "text:..." means TYPE this, not press it — the answer to a question whose
    // real answer is not on the menu ("Type something"). keyboard.press takes one
    // key and would reject a sentence outright, and pressing it character by
    // character gets the shifting wrong on punctuation; keyboard.type is the
    // primitive for a string.
    const TEXT_PREFIX = 'text:';
    for (const key of keys) {
      if (typeof key === 'string' && key.startsWith(TEXT_PREFIX)) {
        await page.keyboard.type(key.slice(TEXT_PREFIX.length));
      } else {
        await page.keyboard.press(NAMED_KEYS[key] || key);
      }
      await page.waitForTimeout(120);
    }
    out({ ok: true, task, sent: keys.length });

  } else {
    fail(`unknown command: ${command}`);
  }
} catch (e) {
  fail(String((e && e.message) || e));
} finally {
  await browser.close();
}
