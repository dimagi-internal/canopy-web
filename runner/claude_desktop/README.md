# canopy desktop runner (`kind=desktop`)

Runs canopy turns as **Claude desktop-app Code sessions** instead of emdash tasks.
Each new thread gets its own git worktree and its own sidebar session in the app.
You can open it, watch it, steer it and stop it. The app's needs-input flags and
diffs work as usual. Design, test evidence and the focus investigation:
[canopy-web#1188](https://github.com/dimagi-internal/canopy-web/issues/1188).

```
ccd_runner.py            the daemon: claim → worktree → seed → import → follow → finish (stdlib only)
mod/                     the `canopy-desktop` Claude Code plugin that runs inside each app session
.claude-plugin/          a marketplace so `claude plugin install` can find the mod
setup-account.sh         one-shot setup of a macOS account as a runner (CLI, mod, pairing, LaunchAgent)
tests/                   unit + loop tests (CI), and fake_harness.py for live runs without labs
```

## Run it on a dedicated macOS account, not the one you work in

A new session is handed to the app with `claude://resume?session=<id>`. That
deep link **activates Claude.app in its own macOS session every time**, even
with `open -g` and even when the app is hidden (measured, #1188 F1). On an
account nobody is looking at (a fast-user-switched runner account), that
activation is invisible and everything works in the background: import,
prompts, tools, follow-ups, waking stopped sessions (#1188 F2–F4). On the
account you are typing in, it would steal focus once per new session.

The account must stay **logged in**. Fast-user-switch away and never log out:
logging out quits the app, and quitting the app stops every session process.

## How a turn runs

1. **Claim.** The runner claims a turn for one of its `projects` or a turn pinned
   to it. It pairs with `sessions: false`, so it never takes other people's
   unbound chats.
2. **New thread.** The runner runs `git worktree add` from the project checkout,
   then writes the worktree's `.claude/settings.local.json` with `permissions.allow`
   and the mod. It seeds `claude -p` there to get a CLI session id, and imports
   that session with the deep link. The app ignores `defaultMode` (it launches
   sessions with `--permission-mode default`), so `allow` rules are what let a turn
   run unattended. The session keeps the seed's model.
3. **Prompt.** The mod finds `<worktree>/.canopy-desktop/` and submits `task.txt`
   as the person's own words. For a continued thread (same `thread_key`), the
   runner writes `fu-<n>.txt` instead. If the mod's `alive` stamp is stale, the
   session's process is down (idle timeout, app restart), and the runner wakes it
   with the same deep link.
4. **Follow.** The runner tails `~/.claude/projects/*/<session>.jsonl` into the
   turn's events and raw transcript. A permission card (`tool.check` → `ask`) is
   posted as `status: needs_input`. On `turn.complete` the runner finishes the
   turn with the answer and `session_key=<cli session id>`.

The mod and the runner talk only through files in `.canopy-desktop/`. The mod
holds no credential, and the directory is excluded from git.

## Set up an account

Do this once, as the account. Claude.app must be installed and **signed in**
first.

```bash
runner/claude_desktop/setup-account.sh --workspace dimagi
```

It asks for a canopy-web token belonging to the **person** who will own the
runner (Settings → Personal access tokens). An agent login can't own a runner,
and only the owner can heartbeat or claim as it.

## Test without labs

```bash
python3 tests/fake_harness.py 8799 &          # stand-in for /api/harness
python3 ccd_runner.py run --config runner.json   # base_url http://127.0.0.1:8799, token "fake"
curl -X POST localhost:8799/api/harness/enqueue \
  -d '{"project":"ccr-scratch","prompt":"…","origin_ref":{"thread_key":"t1"}}'
curl localhost:8799/state
```
