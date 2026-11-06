# Runbook: PostgreSQL major upgrade of `labs-jj-postgres`

How to take the shared labs RDS instance to a new PostgreSQL major, with
downtime. It was rehearsed on a full copy (restored from a production automated
snapshot, same class, storage, subnet group and security group) and then run on
production, 16.13 → 18.6; `labs-jj-postgres` is on 18.6 now. Every timing below
is measured, not estimated. For the next major, re-check §2's target list and
§1's extensions, then follow §4.

Region `us-east-1`, account `858923557655`, profile `labs` (admin SSO;
`canopy aws login --profile labs --reason "..."` if expired).

---

## 1. What is on the instance

`labs-jj-postgres`: db.m6g.large, gp3 100 GB (autoscale to 500), single-AZ, not
public, parameter group `default.postgres16`, option group `default:postgres-16`,
subnet group `labs-jj-db-subnet`, SG `sg-0b8f039813a358f85` (5432 only from the
ECS SG `sg-0666a5ed512c97d9d`), master user `connectadmin`
(Secrets Manager `labs-jj-db-password`).

| Database | Size | Extensions (16 → after upgrade + `ALTER EXTENSION`) | App |
|---|---|---|---|
| `commcare_connect` | 18 GB | postgis 3.4.3 → **3.6.3**, pg_stat_statements 1.10 → 1.12 | connect-labs (`labs-jj-web`, `labs-jj-worker`) |
| `canopy_web` | 1.6 GB | vector 0.8.1 → **0.8.2** (one HNSW index) | canopy-web (`labs-jj-canopy-web`) |
| `umami` | 310 MB | pgcrypto 1.3 → 1.4 | umami (`labs-jj-umami`) |
| `ace_web_labs` | 112 MB | — | ace-web (`labs-jj-ace-web`) |

Nothing else connects to it: `labs-jj-cluster` is the only ECS cluster and runs
exactly those five services; no Lambda names the host. No prepared
transactions, no replication slots, no `reg*` columns in user tables.

**ace-web and connect-labs connect as the master user `connectadmin`.** Never
reset the master password during the cutover: it is their password too.

## 2. Method: in-place `modify-db-instance` straight to 18.6

`describe-db-engine-versions --engine-version 16.13` offers 17.9, 17.10, 17.11,
18.3, 18.4 and 18.6 as major targets, so 16.13 → 18.6 is one hop (no stop at 17).
On the copy it passed RDS's pre-check **without updating PostGIS first**, and
pg_upgrade's only notice was "extensions should be updated".

Considered and not rehearsed:
- **RDS Blue/Green**: cuts downtime to a switchover, but needs a custom
  parameter group with `rds.logical_replication=1` (a reboot of production just
  to prepare) and logical replication of an 18 GB database with PostGIS. Not
  worth it when ~10 minutes of downtime is acceptable.
- **pg_dump / pg_restore into a new 18 instance**: slower for 20 GB and changes
  the endpoint; no advantage over in-place here.

## 3. Measured timings (on the copy)

| Step | Measured |
|---|---|
| Restore snapshot → `available` (also the rollback restore) | 430 s; 428 s |
| `modify-db-instance` → pre-check started | 24 s |
| Pre-check (ran, passed) | 3 s |
| Instance shut down → RDS's own pre-upgrade backup done | 2 m 52 s |
| pg_upgrade (engine upgrade started → finished) | 23 s |
| **Database down** (shutdown → "accepting connections") | **3 m 35 s** |
| `modify` call → accepting connections | 4 m 04 s |
| `modify` call → status `available` (post-upgrade backup + monitoring config, online) | 8 m 16 s |
| An RDS backup of this instance (three separate runs) | 2 m 33 s – 2 m 52 s |
| `ALTER EXTENSION … UPDATE` / `postgis_extensions_upgrade()`, all DBs | < 5 s |
| `ANALYZE` — ace_web_labs / canopy_web / commcare_connect / umami | 9.9 s / 2 m 01 s / **12 m 13 s** / 18.0 s |
| Rename an instance (`--new-db-instance-identifier`) → `available` + DNS | 79 s |

**On production** the numbers held except one:

| Step | Production |
|---|---|
| Scale five services to 0 (`services-stable`) | 22 s |
| **Manual snapshot, 12 h after the last automated backup** | **26 m 38 s** |
| Pre-check / database down (shutdown → accepting connections) | 2 s / 3 m 15 s |
| Extension updates | < 5 s |
| Scale back up → `services-stable` | 2 m 39 s |
| `ANALYZE`, all four databases, online | ≈ 54 s (commcare_connect 47 s) |

The rehearsal copy had been backed up minutes before its upgrade, so its
snapshots were incremental and fast; production's carried twelve hours of
changes. Step 2 below now takes that snapshot **before** the outage. `ANALYZE`
was 15× faster on production than on the copy, because a freshly restored
instance loads its blocks lazily from S3.

**PostgreSQL 18 keeps planner statistics through pg_upgrade** (`pg_stats` was
populated in every database before any `ANALYZE`), so the 12-minute `ANALYZE` of
`commcare_connect` runs *after* the apps are back, not inside the outage.

Status goes `upgrading` → `backing-up` → `configuring-enhanced-monitoring` →
`available`; the database accepts connections from the RDS event
"The DB instance is running and accepting connections", ~4 minutes before
`available`.

## 4. Cutover

Order of operations, each command as run. Set up once:

```bash
export AWS_PROFILE=labs AWS_REGION=us-east-1
DB=labs-jj-postgres; C=labs-jj-cluster
SERVICES="labs-jj-web labs-jj-worker labs-jj-ace-web labs-jj-canopy-web labs-jj-umami"
SNAP=labs-jj-postgres-pre-upgrade-$(date -u +%Y%m%d-%H%M)
```

**0. Pre-flight** (before announcing downtime):

```bash
aws rds describe-db-instances --db-instance-identifier $DB \
  --query 'DBInstances[0].[EngineVersion,DBInstanceStatus,PendingModifiedValues]'   # 16.x, available, {}
aws rds describe-db-engine-versions --engine postgres --engine-version \
  $(aws rds describe-db-instances --db-instance-identifier $DB --query 'DBInstances[0].EngineVersion' --output text) \
  --query 'DBEngineVersions[0].ValidUpgradeTarget[?IsMajorVersionUpgrade==`true`].EngineVersion'   # 18.6 listed
aws ecs describe-services --cluster $C --services $SERVICES \
  --query 'services[].[serviceName,desiredCount]' --output text > /tmp/pg18-desired-counts.txt   # record to restore
```

If AutoMinorVersionUpgrade has moved production off 16.13, re-check that the
target is still offered. Don't deploy any of the five apps during the window.

**1. Stop the writers** (all five services):

```bash
for s in $SERVICES; do aws ecs update-service --cluster $C --service $s --desired-count 0 >/dev/null; done
aws ecs wait services-stable --cluster $C --services $SERVICES
```

`update-service --desired-count` only scales: CloudFormation still owns the task
definitions (see `docs/architecture/deploy.md`); don't register task
definitions by hand.

**2. A named manual snapshot, taken twice: once online, once in the outage.**
Snapshots are incremental, and the cost is the change since the last one: 26 m
38 s on production when the last backup was 12 h old, against ~2.5–3 min right
after a backup. So take one **before step 1, with the apps still up**, then the
real one after step 1, which only has to copy the minutes in between:

```bash
aws rds create-db-snapshot --db-instance-identifier $DB --db-snapshot-identifier $SNAP-warm
aws rds wait db-snapshot-available --db-snapshot-identifier $SNAP-warm      # apps still up
# ... step 1 (scale to 0) ...
aws rds create-db-snapshot --db-instance-identifier $DB --db-snapshot-identifier $SNAP \
  --tags Key=purpose,Value=pre-major-upgrade
aws rds wait db-snapshot-available --db-snapshot-identifier $SNAP           # the rollback point
```

The instance is `backing-up` while a snapshot runs and the upgrade can't start
until it ends. RDS also takes its own `rds:preupgrade-…` snapshot in step 3.
Keep the named one anyway: you control its name and it doesn't expire with the
automated-backup window. Delete `$SNAP-warm` afterwards.

A manual deploy of any of the five apps during the window recreates its service
at its CloudFormation count (a canopy-web deploy did, mid-snapshot). Re-check the
counts right before step 3 and scale back to 0 if anything came up.

**3. Upgrade** (database down ~3.5 min, measured):

```bash
aws rds modify-db-instance --db-instance-identifier $DB --engine-version 18.6 \
  --allow-major-version-upgrade --db-parameter-group-name default.postgres18 --apply-immediately
# watch for "accepting connections" (≈4 min) rather than waiting for `available` (≈8 min):
aws rds describe-events --source-identifier $DB --source-type db-instance --duration 30 \
  --query 'Events[].[Date,Message]' --output text
```

The option group moves to `default:postgres-18` by itself. If the pre-check
fails, the instance stays on 16 untouched: read the event and the
`error/pg_upgrade*` log files (`aws rds describe-db-log-files`,
`download-db-log-file-portion`), and scale the services back up.

**4. Update extensions** (as `connectadmin`, from inside the VPC; see §6 for how):

```sql
\c canopy_web
ALTER EXTENSION vector UPDATE;                 -- 0.8.1 → 0.8.2
\c commcare_connect
SELECT postgis_extensions_upgrade();           -- 3.4.3 → 3.6.3
ALTER EXTENSION pg_stat_statements UPDATE;     -- 1.10 → 1.12
\c umami
ALTER EXTENSION pgcrypto UPDATE;               -- 1.3 → 1.4
```

**5. Scale back up** to the counts recorded in step 0 (2, 1, 2, 1, 1 when this was written):

```bash
while read s n; do aws ecs update-service --cluster $C --service $s --desired-count $n >/dev/null; done < /tmp/pg18-desired-counts.txt
aws ecs wait services-stable --cluster $C --services $SERVICES
```

**6. `ANALYZE`, online** (statistics already carried over, so this only
refreshes them; ~15 min, almost all of it `commcare_connect`):

```sql
\c ace_web_labs
ANALYZE;
\c canopy_web
ANALYZE;
\c umami
ANALYZE;
\c commcare_connect
ANALYZE;
```

**7. Smoke tests:**

```bash
curl -fsS https://canopy.dimagi.com/health/
aws rds describe-db-instances --db-instance-identifier $DB \
  --query 'DBInstances[0].[EngineVersion,DBParameterGroups[0].DBParameterGroupName,DBInstanceStatus]'   # 18.6, default.postgres18
```

Then load a page in each app (canopy-web, the connect-labs dashboard, ace-web,
umami), and run `SELECT extname, extversion FROM pg_extension` in each database
(expected versions in §1). Within a day, check the instance's
`error/postgresql.log.*` for new `ERROR` lines.

## 5. Rollback

Data written after the snapshot is lost; use it only if the apps can't run on 18.

```bash
for s in $SERVICES; do aws ecs update-service --cluster $C --service $s --desired-count 0 >/dev/null; done
# free the name (79 s measured), then restore the 16 snapshot under the original name (428 s measured)
aws rds modify-db-instance --db-instance-identifier $DB --new-db-instance-identifier $DB-pg18-failed --apply-immediately
aws rds wait db-instance-available --db-instance-identifier $DB-pg18-failed
aws rds restore-db-instance-from-db-snapshot --db-instance-identifier $DB --db-snapshot-identifier $SNAP \
  --db-instance-class db.m6g.large --db-subnet-group-name labs-jj-db-subnet \
  --vpc-security-group-ids sg-0b8f039813a358f85 --availability-zone us-east-1a \
  --no-publicly-accessible --no-multi-az --storage-type gp3 \
  --db-parameter-group-name default.postgres16 \
  --engine-lifecycle-support open-source-rds-extended-support --copy-tags-to-snapshot
aws rds wait db-instance-available --db-instance-identifier $DB
# then scale the services back up (step 5); keep $DB-pg18-failed until satisfied, then delete it
```

Keeping the name keeps every `DATABASE_URL` secret valid, so no secret or task
definition changes. Afterwards, `modify-db-instance` the restored instance back
to `--max-allocated-storage 500`, 7-day backup retention, the Mon 04:57–05:27
UTC maintenance window and `--auto-minor-version-upgrade` if the restore didn't
carry them. Total
≈ 8.5 min plus scaling.

## 6. Running SQL inside the VPC

The instance isn't public. What worked in the rehearsal: a throwaway Fargate
task (`postgres:18` image, entryPoint `bash -c`, the SQL as its command, logs
to a temporary log group) in `subnet-06646effb09be2f42` with SG
`sg-0666a5ed512c97d9d` and a public IP for the image pull. That SG is already
allowed in, so no rule changes are needed. Pass `PGHOST`, `PGUSER=connectadmin`
and `PGPASSWORD` (from `labs-jj-db-password`) as container overrides, and
delete the task definition and log group afterwards. ECS Exec into the
canopy-web task also works (how the `vector` extension was first created, in
`docs/architecture/deploy.md`).

## 7. What each app needs

Each app's **current production image** was run against the upgraded copy
(DATABASE_URL pointed at the copy inside the container, no task role):

| App | Driver | Result on 18.6 |
|---|---|---|
| canopy-web | psycopg 3.3.8, Django 6.1.3 | connects (`pg_version 180006`); 253/254 models read. The one miss is a migration merged after the snapshot, not PG18 |
| connect-labs (web + worker image) | psycopg2 2.9.13, Django 6.0.8 | connects; 211/211 models read; PostGIS geography ops work; the one unapplied migration merged after the snapshot |
| ace-web | psycopg 3.3.3 (pool), Django 5.2.13 | connects; `migrate --check` clean; 31/31 models read |
| umami v3.3.1 | Prisma | `check-db`: connection and version check OK, "No pending migrations"; server starts |

**No driver, ORM or settings change is needed for the cutover.** The default
parameter group for 18 adds `client_connection_check_interval=60000`,
`io_method=sync` and `allow_alter_system=0`; `rds.force_ssl=1` is unchanged.

**After the cutover**, so local and CI match production again (canopy-web's are
done):

- **canopy-web**:
  - `pgvector/pgvector:pg16` → `pgvector/pgvector:pg18` in `.github/workflows/ci.yml` (the service image, the `pgc` pg_dump/pg_restore image, and the second service at the end), `.github/workflows/pg-nightly.yml`, `.github/workflows/contract-nightly.yml` and `docker-compose.yml`.
  - Bump the migrated-test-DB cache key `pg-testdb-v2-` → `pg-testdb-v3-` in `ci.yml`, because a pg16 dump must not be restored by the pg18 client path or the other way round. `pg-nightly.yml`'s key already includes the image id.
  - Update the comment above the cache step and the "PostgreSQL 16.13, pgvector 0.8.1" line in `docs/architecture/deploy.md`.
- **connect-labs**: CI runs `postgis/postgis:17-3.5` (mirrored by `mirror-ci-images.yml`); production is 18 with PostGIS 3.6.3, so move it to the 18 / PostGIS 3.6 tag and mirror it.
- **ace-web**: `docker-compose.yml` `postgres:16` → `postgres:18` (local only).
- **umami**: nothing.
