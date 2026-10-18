# Walkthrough Sharing — Design Spec

**Date:** 2026-05-26
**Status:** Draft — awaiting implementation plan
**Repo:** canopy-web

## Problem

`/canopy:walkthrough` (the canopy plugin skill) produces high-quality HTML
slideshows of live-app demos — screenshots, AI quality scores, narrative.
Today those artifacts land on disk inside whatever worktree the skill ran in
(e.g. `screenshots/walkthroughs/ace-web-demo.html`, `evals/walkthrough/source/connect/index.html`).
There is no shared place to send a walkthrough to a teammate or a client; the
file has to be emailed, dropped in Slack, or hosted ad-hoc.

Separately, some demos are best as recorded video (screen recording, narrated
flow). Today those have no home in canopy at all.

We want canopy-web to be the share-hub for both: a teammate generates a
walkthrough, opts in to upload, and gets back a URL they can paste anywhere.

## Goals

- One URL per walkthrough, served from canopy-web.
- Two kinds in v1: **HTML slideshow** (from `/canopy:walkthrough`) and **MP4 video**
  (uploaded manually).
- Per-walkthrough visibility: **private** (dimagi OAuth gate) or **link**
  (anyone with the unguessable `?t=<token>` URL).
- Opt-in upload from the canopy CLI plugin — no auto-upload.
- Optional linkage to an existing canopy-web project, surfaced on the workbench tile.
- Owner can rotate or revoke the share token; deletion is one click.

## Non-Goals (Deferred)

Logged in `TODOS.md` post-spec:
- View analytics (who, when, where from)
- Multi-link / per-audience tokens
- Comments / reactions on walkthroughs
- Embeds (oEmbed)
- Video poster frames, chapter markers, thumbnails
- Signed Drive URLs for video (approach B below) — only revisit if Cloud Run
  egress becomes a measurable cost
- Auto-upload mode in `/canopy:walkthrough`

## Architecture

```
┌───────────────────────┐   upload    ┌─────────────────────┐
│  /canopy:walkthrough  ├────────────►│  canopy-web API     │
│  (HTML output)        │             │  POST /walkthroughs │
│  + /walkthrough-share │             └─────────┬───────────┘
│  CLI skill            │                       │
└───────────────────────┘                       ▼
              ┌───────────────────────────────────────┐
              │ Postgres `walkthroughs` row           │
              │  ├─ kind: html | video                │
              │  ├─ project_slug (nullable)           │
              │  ├─ visibility: private | link        │
              │  ├─ share_token (nullable, 32 char)   │
              │  └─ drive_file_id                     │
              └───────────────────────────────────────┘
                                                 │
                                                 ▼
              ┌───────────────────────────────────────┐
              │ Google Drive (canopy SA, shared dir)  │
              │  walkthroughs/<uuid>/                 │
              │    ├─ slideshow.html                  │
              │    └─ video.mp4                       │
              └───────────────────────────────────────┘

GET /w/<id>?t=<token>  →  Django checks auth → streams from Drive
```

Three independent units:

1. **`apps/walkthroughs/`** — Django app: model, REST endpoints, Drive client
   (copied from ace-web's `apps/opps/drive_client.py` pattern, slimmed).
2. **Frontend** — `/walkthroughs` global feed, project-tile section, `/w/<id>` viewer.
3. **CLI skill** in the canopy plugin — `canopy:walkthrough-share` (new) +
   opt-in prompt from existing `canopy:walkthrough`.

### Approach Selected

**A — Proxy everything.** Drive files stay private (SA-owned in a shared
drive). Django streams every byte on view. Chosen for full auth control,
simplest mental model, easy revoke.

**B — Hybrid (proxy HTML, signed URLs for video)** was the runner-up; deferred
until egress becomes a real cost.

**C — Drive `anyoneWithLink`** was rejected: share URLs would be Drive URLs
(off-brand, no canopy-web analytics), private walkthroughs would be unreachable
to non-dimagi viewers, revoke is slow.

## Data Model

One model. Share-link state lives on the row.

```python
class Walkthrough(models.Model):
    id              = UUIDField(primary_key=True, default=uuid4)
    title           = CharField(max_length=200)
    description     = TextField(blank=True)
    kind            = CharField(choices=[('html', 'HTML'), ('video', 'Video')])
    project_slug    = CharField(max_length=200, blank=True, null=True, db_index=True)
    owner           = ForeignKey(User, on_delete=PROTECT)
    visibility      = CharField(choices=[('private', 'Private'), ('link', 'Link')], default='private')
    share_token     = CharField(max_length=32, blank=True, null=True, unique=True)
    drive_file_id   = CharField(max_length=128)
    drive_folder_id = CharField(max_length=128)
    content_type    = CharField(max_length=64)
    size_bytes      = BigIntegerField()
    duration_sec    = IntegerField(null=True, blank=True)   # video only
    created_at      = DateTimeField(auto_now_add=True)
    updated_at      = DateTimeField(auto_now=True)
```

Rationale for **no separate `ShareLink` table**: v1 only needs one link per
walkthrough. Promote to its own table when we want multi-link or per-audience
analytics (deferred).

`project_slug` is a string FK (matches `Project.slug`) to mirror the existing
loose-coupling pattern in `apps/projects/` (e.g., context entries reference
projects by slug). Validated on write; nullable.

`share_token` is generated with `secrets.token_urlsafe(24)` (~32 chars).
`unique=True` so token rotation can't accidentally collide. Null when
`visibility=private`.

**Single-tenant V1 list semantics:** any authenticated dimagi user can list
and view every walkthrough — including others' private ones. This mirrors
how the rest of canopy-web treats projects and skills today. Multi-tenant
hardening is tracked in `TODOS.md` alongside the rest of the multi-tenant
roadmap.

## API

All paths under `/api/walkthroughs/` unless noted.

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| `POST` | `/api/walkthroughs/` | Upload (multipart: `file` + JSON `metadata`) | Upload token OR dimagi session |
| `GET` | `/api/walkthroughs/` | List. Filters: `?project=<slug>`, `?kind=html`, `?mine=true` | Dimagi session |
| `GET` | `/api/walkthroughs/<id>/` | Metadata | Dimagi session, OR token if visibility=link |
| `PATCH` | `/api/walkthroughs/<id>/` | Update title/description/project/visibility | Owner only |
| `DELETE` | `/api/walkthroughs/<id>/` | Delete row + Drive file | Owner only |
| `POST` | `/api/walkthroughs/<id>/rotate-token/` | Mint new `share_token` | Owner only |
| `GET` | `/w/<id>` | Viewer page (frontend route, served by SPA) | n/a (page) |
| `GET` | `/w/<id>/content` | Stream file bytes from Drive | Same as metadata GET |

### Upload auth

Reuse the existing `/api/auth/e2e-login/` token pattern. New env var
`CANOPY_WALKTHROUGH_UPLOAD_TOKEN` (separate from `CANOPY_E2E_AUTH_TOKEN` so
revoking one doesn't kill the other). When the upload endpoint sees a valid
`Authorization: Bearer <token>` header, it resolves the request to a fixed
service user — same mechanism the e2e login uses.

When the request has both a session cookie and a bearer token, prefer the
session — that's the interactive-upload path (drag-drop in `/walkthroughs`,
deferred to v1.1 if needed).

### Streaming

`/w/<id>/content` uses `StreamingHttpResponse` with chunked reads from Drive
(`drive_client.download(file_id, range=...)`). HTTP `Range` header passes
through to Drive — required for `<video>` scrubbing.

Initial chunk size: 1 MB. Revisit after first user reports if playback stutters.

## CLI Skill (canopy plugin)

New skill `canopy:walkthrough-share`. Lives in the canopy plugin repo
(`~/.claude/plugins/marketplaces/canopy/plugins/canopy/skills/walkthrough-share/SKILL.md`),
not in canopy-web.

```
/canopy:walkthrough-share <path>
/canopy:walkthrough-share <path> --project canopy-web --title "Skill Builder Demo"
/canopy:walkthrough-share <path> --public
```

Behavior:

1. Read `~/.canopy/config` for `canopy_web_url` + `upload_token`.
   (Existing config; add two new keys, document in `canopy:setup`.)
2. Detect kind by extension: `.html` → html, `.mp4` → video, else reject.
3. **HTML inline pass:** parse the HTML, find `<img src="X">` and CSS
   `url(X)` references with relative paths, base64-encode the referenced files
   into data URIs, produce one self-contained `.html` blob in a temp dir.
   Cap inline result at 50 MB; if exceeded, fail loud and tell the user to
   shrink screenshots (deferred: tarball mode).
4. POST multipart to `/api/walkthroughs/`. Bearer-token auth.
5. Print:
   - `View: https://canopy.dimagi.com/w/<id>`
   - if `--public`: `Share: https://canopy.dimagi.com/w/<id>?t=<token>`

Also: extend existing `canopy:walkthrough` skill with a trailing prompt —
*"Upload this walkthrough to canopy-web? [y/N]"* — that shells to
`canopy:walkthrough-share` on yes. Opt-in only. (This change lands in the
canopy plugin repo, not in this canopy-web repo. Track as a follow-up.)

## Frontend

Built in the existing React + Vite + Tailwind app.

### `/walkthroughs`

Dense table (matches `/skills` density):

| Title | Project | Kind | Owner | Visibility | Size | Created |
|-------|---------|------|-------|-----------|------|---------|

Filters in the page header: project select, kind toggle, "Mine only" checkbox.
Row click → `/w/<id>`.

### Project tile (workbench)

In the expanded project card, add a one-line link in the right column:
"Walkthroughs · N" linking to `/walkthroughs?project=<slug>`. If `N=0`, no
link rendered. Adds one query to the project detail endpoint (cheap COUNT).

### `/w/<id>` viewer

Single page. Top: title, owner, kind chip, visibility chip. Body:

- `kind=html` → `<iframe sandbox="allow-scripts allow-same-origin" src="/w/<id>/content?t=...">`
- `kind=video` → `<video controls src="/w/<id>/content?t=...">` (range-served)

If the viewer is the owner, show a toolbar:
- Visibility toggle (Private ↔ Link)
- "Copy share link" — mints token if missing
- "Rotate token" — invalidates old, mints new, copies to clipboard
- "Delete" — confirm dialog, then DELETE

Non-owners see just the player.

## Drive Layout & Service Account

Reuse ace-web's pattern verbatim.

```
<canopy-web shared drive root>/
  walkthroughs/
    <walkthrough-uuid>/
      slideshow.html    # kind=html
      video.mp4         # kind=video
```

- New module `apps/walkthroughs/drive_client.py`. Copy `apps/opps/drive_client.py`
  from ace-web; trim to the methods used here (`upload_file`, `update_file`,
  `delete_file`, `get_content` / chunked `download`). Keep the `_drive_retry`
  decorator on reads.
- One env var, no service-account registry: `CANOPY_DRIVE_SA_KEY_JSON`
  (JSON string), parsed at startup. Symmetric with how ace-web exposes
  `ACE_DRIVE_SA_KEY_JSON`. If we ever add a second SA-backed feature, we
  port ace-web's `apps/service_accounts/` registry then.
- One env var for the folder: `CANOPY_DRIVE_ROOT_FOLDER_ID`. Per-walkthrough
  subfolders are created on demand under it.
- The SA must be invited to the Shared Drive folder as Content Manager.

### Deployment prereqs (call out in `docs/deploy.md`)

1. Create a Shared Drive folder ("canopy-web walkthroughs" under the dimagi
   shared drive).
2. Generate SA key (or reuse ACE SA if it has access to the right folder),
   put JSON in 1Password.
3. Set Cloud Run env vars: `CANOPY_DRIVE_SA_KEY_JSON`, `CANOPY_DRIVE_ROOT_FOLDER_ID`,
   `CANOPY_WALKTHROUGH_UPLOAD_TOKEN`.

If `CANOPY_DRIVE_SA_KEY_JSON` is empty, upload + view endpoints return a 500
with `code="drive-not-configured"` (same affordance as ace-web's
`drive-not-configured` error).

## Error Handling

- **Upload, Drive write fails after row creation:** delete the row in the
  same request, return 502 with `code="drive-upload-failed"`. No orphan rows.
- **View, Drive download fails:** return 502 with `code="drive-read-failed"`;
  the viewer page surfaces a "playback unavailable, try again" message.
- **Drive 5xx/429:** the inherited `_drive_retry` decorator handles read
  retries (3 attempts, exponential backoff). Writes do not retry — duplicate
  uploads would leak Drive files.
- **Token mismatch on `/w/<id>/content`:** return 404, not 403. Don't leak
  existence of private walkthroughs.

## Testing

Backend (pytest):
- Model: token generation, uniqueness, owner-only mutation guards.
- Upload endpoint: bearer token gate, multipart parsing, kind detection,
  Drive write failure rolls back row.
- View endpoint: session-only access for private, token+session for link,
  404 on token mismatch, Range header passthrough.
- Drive client: mock the googleapiclient layer (mirror ace-web's
  `FakeDriveClient` fixture).

Frontend:
- The walkthrough viewer is a single page over an `<iframe>` / `<video>` —
  Playwright smoke that the page renders and the toolbar appears for owner only.
  Defer richer UI testing.

Manual:
- Upload an HTML walkthrough from `/canopy:walkthrough-share` against a local
  dev instance. View, rotate token, delete.
- Upload a small `.mp4` (~10 MB), confirm video plays with scrubbing.

## Migration / Rollout

- One Django migration creating the `walkthroughs` table.
- Ship behind a settings flag `WALKTHROUGHS_ENABLED` (default `True`).
  Endpoints return 404 when off — same pattern as `/api/auth/e2e-login/`.
- No data migration needed; this is greenfield.

## Open Questions

None blocking. Items to revisit post-v1:
- Do we want a "featured" flag for the `/walkthroughs` feed (default sort)?
- Do uploads from the CLI dedupe by content hash? (Probably yes — prevents
  re-uploading the same demo per run; defer.)
- Should `project_slug` be a hard FK with `ON DELETE SET NULL` once we have
  more confidence in the projects table?

## File Layout

New code:

```
apps/walkthroughs/
  __init__.py
  apps.py
  models.py             # Walkthrough
  views.py              # REST endpoints
  drive_client.py       # ported from ace-web
  urls.py
  serializers.py
  tests/
    test_models.py
    test_views.py
    test_drive_client.py
    fixtures/
      fake_drive.py     # ported from ace-web
  migrations/
    0001_initial.py

frontend/src/
  pages/
    Walkthroughs.tsx    # list page
    WalkthroughViewer.tsx  # /w/<id>
  components/walkthroughs/
    WalkthroughTable.tsx
    WalkthroughToolbar.tsx
    UploadDialog.tsx    # v1.1 — drag-drop upload UI; not in v1
```

Settings additions in `config/settings/base.py`:
- `WALKTHROUGHS_ENABLED`
- `CANOPY_DRIVE_SA_KEY_JSON`
- `CANOPY_DRIVE_ROOT_FOLDER_ID`
- `CANOPY_WALKTHROUGH_UPLOAD_TOKEN`

`INSTALLED_APPS` += `apps.walkthroughs`.
`urls.py` += `path('api/walkthroughs/', include('apps.walkthroughs.urls'))`.
SPA routes `/walkthroughs` and `/w/:id` registered in `frontend/src/App.tsx`.

## Out-of-Repo Work (track separately)

- Canopy plugin: new `canopy:walkthrough-share` skill, edit existing
  `canopy:walkthrough` to add the opt-in upload prompt, extend `canopy:setup`
  to write `upload_token` + `canopy_web_url` to `~/.canopy/config`.
- Deployment: Drive folder + SA grant + Cloud Run env vars (see
  "Deployment prereqs" above).

## References

- `apps/opps/drive_client.py` (ace-web) — Drive client pattern being copied.
- `apps/videos/drive.py` (ace-web) — Drive folder-layout pattern.
- `apps/common/views_auth_e2e.py` (this repo) — bearer-token auth pattern.
- `docs/walkthroughs/canopy-web-demo.yaml`, `docs/walkthroughs/project-workbench.yaml`
  — example walkthrough source specs.
