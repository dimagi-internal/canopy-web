# canopy-cron

The Django-free **cron slot math** for canopy-web's scheduled agent turns,
extracted from `apps/harness/cron.py`.

## Why this is a package

The scheduler is split across two processes, and the split runs straight through
the cron math:

- The **server** (`apps/harness`) holds the schedule config and previews the next
  fire times — `next_slots` is what makes a raw cron expression trustworthy in
  the editor without a docs trip.
- The **runner** (`runner/canopy_runner`) evaluates the cron and POSTs the due
  slot — `due_slot` exists for it. The runner is Django-free by construction
  (`dependencies = []`) and can never import `apps.*`.

So the module belongs to neither side. Left in `apps/harness`, the runner would
have to reimplement `due_slot` — and a second cron implementation is exactly the
"the UI says Fridays but it fires Thursdays" divergence the scheduled-turns
design exists to prevent. Both sides now import the same functions, and a fix to
the DST handling can only land in one place.

## What's in here

- `canopy_cron.validate_cron` / `validate_timezone` — reject at edit time. A cron
  typo that silently never fires is the worst failure a scheduler has.
- `canopy_cron.due_slot` — which slot is due: at most one, never a backfill.
  Three weeks offline yields the newest occurrence only (the supersede rule
  applied at firing time).
- `canopy_cron.next_slots` — preview the next N fire times.

All datetimes in and out are timezone-aware UTC; the local wall-clock
interpretation happens inside, against the schedule's IANA zone.

## Django-free guarantee

`import canopy_cron` imports only `datetime`, `zoneinfo`, and `croniter`. No
Django, no `django.conf.settings`, no models.

## Install

```bash
pip install canopy-cron
```

The `croniter` bound (`>=6.0,<7.0`) is pinned here deliberately: a major bump
changing `get_prev()` / DST semantics would mean "the schedule silently never
fires."

## Tests

```bash
cd packages/canopy_cron && pytest -q   # plain pytest, no Django, no DB
```

The DST cases are the valuable part — 9am ET must stay 9am ET across the shift,
and `due_slot` and `next_slots` do their zone conversion independently, so both
are pinned.
