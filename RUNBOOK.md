# PlacePrep AI backend — runbook

Phase 1. Sections marked **TODO** are filled in during Phase 5.

---

## Local development

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements-dev.txt
cp .env.example .env                                 # then fill it in
python -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET
alembic upgrade head
python -m scripts.seed
uvicorn app.main:app --reload --port 8000
```

`http://localhost:8000/docs` has the interactive API (disabled in production).

Without Redis configured the key-value store falls back to an in-process dict. That is
fine for one worker and nothing else — `/ready` reports it explicitly.

Tests: `pytest -q`. Lint: `ruff check .`

---

## Backing up before a migration

Project `nybywdmmlzgzncvrstoa` (region `ap-southeast-1`) holds real data: 9 users,
67 interview sessions, 173 transcripts, 83 credit transactions. Do **both** steps —
the dashboard backup is the fast restore path, the dump is the one that works if the
project itself is unavailable.

### 1. Confirm the dashboard backup

Supabase Dashboard → Project → **Database → Backups**.

- Check there is a backup dated **today**. Note its timestamp; that is the point you can
  restore to.
- On Free tier, daily backups are the only option and there is no on-demand button — the
  `pg_dump` in step 2 is then your *only* pre-migration snapshot, so do not skip it.
- On Pro and above, also check whether Point-in-Time Recovery is on. If it is, note the
  current time instead; PITR can restore to the second.

### 2. Take your own dump

`pg_dump` needs a **session**-mode connection. The transaction pooler on port 6543
multiplexes statements across backends and will fail or produce an inconsistent dump.

Copy the **Session pooler** URI from Dashboard → Project Settings → **Database** →
Connection string → URI (it looks like
`postgresql://postgres.nybywdmmlzgzncvrstoa:[YOUR-PASSWORD]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres`).
Port **5432**, not 6543.

```powershell
# Paste the Session pooler URI, with your real database password.
# Prompted, so the password does not land in your shell history.
$env:PGPASSWORD = Read-Host -AsSecureString "DB password" | ForEach-Object { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($_)) }
```

```powershell
pg_dump --format=custom --no-owner --no-privileges --verbose --schema=public --file="D:\PlacePrep Ai Updated\backups\placeprep-pre-phase1-$(Get-Date -Format yyyyMMdd-HHmm).dump" --host=aws-0-ap-southeast-1.pooler.supabase.com --port=5432 --username=postgres.nybywdmmlzgzncvrstoa --dbname=postgres
```

Also take a plain-SQL copy — it is readable, greppable, and restorable with `psql` if
`pg_restore` versions ever disagree:

```powershell
pg_dump --format=plain --no-owner --no-privileges --schema=public --file="D:\PlacePrep Ai Updated\backups\placeprep-pre-phase1-$(Get-Date -Format yyyyMMdd-HHmm).sql" --host=aws-0-ap-southeast-1.pooler.supabase.com --port=5432 --username=postgres.nybywdmmlzgzncvrstoa --dbname=postgres
```

### 3. Verify the dump is real before trusting it

A zero-byte or truncated dump is worse than none, because it looks like a backup.

```powershell
Get-ChildItem "D:\PlacePrep Ai Updated\backups\" | Select-Object Name, Length, LastWriteTime
```

```powershell
pg_restore --list "D:\PlacePrep Ai Updated\backups\<your-file>.dump" | Select-String "TABLE DATA" | Measure-Object
```

Expect roughly 32 `TABLE DATA` entries. Then confirm the rows that matter are in there:

```powershell
Select-String -Path "D:\PlacePrep Ai Updated\backups\<your-file>.sql" -Pattern "COPY public.(students|interview_sessions|session_transcripts|credit_transactions)" | Select-Object Line
```

Finally, clear the password from the session:

```powershell
Remove-Item Env:PGPASSWORD
```

`backups/` is gitignored. Keep a copy somewhere other than this laptop.

### Restoring

```powershell
pg_restore --clean --if-exists --no-owner --no-privileges --dbname="<session pooler URI>" "D:\PlacePrep Ai Updated\backups\<your-file>.dump"
```

`--clean` drops the existing objects first. On the live project that is destructive, so
only do it on a deliberate decision to roll the database back.

---

## Applying migrations

**Do the backup above first.** The steps below change live data.

```bash
# 1. Review what will run (0004 renders only as part of a range, see note):
alembic upgrade 0002_phase1_foundation --sql | less
# 2. The existing schema was never migrated, so record it as the baseline:
alembic stamp 0001_baseline
# 3. Apply:
alembic upgrade head
# 4. Verify:
alembic current          # expect 0006_fk_covering_indexes
# 5. Seed plans, JD templates and the retrieval chunks:
python -m scripts.seed
```

Notes:

- `0001_baseline` must be **stamped**, not run, on the existing project. It only exists so
  a fresh environment can be built from migrations.
- `0002` is additive except `student_credits.updated_at` TEXT → TIMESTAMP (approved).
- `0003` moves data (credits → token ledger) and refuses `--sql` mode; it needs a live
  connection. To render later migrations as SQL, use a range:
  `alembic upgrade 0003_backfill_tokens:0004_rls_deny_by_default --sql`.
- `0004` enables RLS with **no policies** on all 32 public tables and revokes the
  `anon` / `authenticated` grants. It deliberately does not use
  `FORCE ROW LEVEL SECURITY`: the backend connects as the table owner, and forcing it
  with no policies would deny the application everything.
- `student_credits` and `credit_transactions` are **kept** as the historical record.
  Nothing reads them after `0003`.

After applying, re-run the Supabase security and performance advisors and expect the
`rls_disabled_in_public` findings to be gone.

**Two advisor findings are expected to remain, and must not be "fixed":**

- `rls_enabled_no_policy` (INFO, 32 tables). This *is* the intended state: RLS on with
  no policies means deny-by-default for `anon` and `authenticated`. The advisor flags it
  because the usual cause is someone enabling RLS and forgetting policies, locking their
  own client app out. Here there is no client app on that path. **Adding policies to
  silence this would grant the anon key access and undo the fix.**
- `unused_index` (INFO). Immediately after a migration this only means no traffic has
  reached the new tables yet. Re-check after a real end-to-end run before considering any
  index for removal.

### State of the live database (applied 2026-11-27)

| | |
|---|---|
| `alembic_version` | `0006_fk_covering_indexes` |
| Student wallets | 8, totalling 472 tokens |
| College pool | 1 wallet, 457 tokens, expires in 365 days |
| Students linked to a college | 7 (2 platform admins deliberately unscoped) |
| Default allowance group | 1, with 6 student members at 4/month |
| `jd_chunks` | 6 — one per seeded role, so JD retrieval returns requirements again |
| `plans` | 7 |
| RLS | 32 tables enabled, 0 policies, 0 `anon` / `authenticated` grants |

One `student_credits` row holding 3 credits was **not** migrated: its `student_id` has no
matching row in `students`. The Alembic migration joins to `students` and skips it too, so
472 rather than 475 is the correct, faithful result. Nothing to chase unless that account
is meant to exist.

Rollback: `alembic downgrade 0001_baseline` undoes `0002`, `0003` and `0004`.
`0001` refuses to downgrade on purpose — it would drop every interview on the platform.

### Lost authenticator (admin TOTP)

There is no self-service reset, by design. Another admin clears the factor directly:

```sql
UPDATE students SET mfa_enabled = false, mfa_secret = NULL, token_version = token_version + 1
WHERE email = '<locked-out admin>';
```

Bumping `token_version` signs that account out everywhere, so they must sign in again and
re-enrol at `/admin → Security`.

---

## Deploying

**TODO (Phase 5):** Vercel project for the backend, `api.placeprepai.fun`, per-environment
variables, preview vs production separation.

Before the first production deploy, every variable marked `[required]` in `.env.example`
must be set — the app refuses to start otherwise, which is deliberate. `GET
/api/super/config-check` (admin only) lists what is and is not configured without
revealing any value.

---

## Rotating keys

| Key | Effect of rotating | Steps |
|---|---|---|
| `JWT_SECRET` | **Signs everyone out.** | Set the new value, redeploy. Users log in again. |
| `GROQ_API_KEY` | None if `GROQ_API_KEY2` is set — it is the fallback. | Rotate the primary, verify, then rotate the secondary. |
| `RAZORPAY_*` | Orders in flight fail. | Rotate out of business hours; re-check the webhook secret in the Razorpay dashboard. |
| `SUPABASE_SERVICE_ROLE_KEY` | Resume and snapshot storage stops. | Rotate in Supabase, update the env var, redeploy. |
| `GITHUB_TOKEN` | Profile enrichment falls back to 60 requests/hour per IP. | Create a new fine-grained token (public-repo read only), swap, delete the old one. |

To sign out one user rather than everyone: increment `students.token_version`.

---

## Incidents

### "Interviews are failing"

1. `GET /ready` — is the database or Redis down?
2. Look for `llm_provider_failed` / `llm_structured_parse_failed` in the logs.
3. `SELECT task, model, ok, count(*) FROM llm_usage WHERE created_at > now() - interval '1 hour' GROUP BY 1,2,3;`
4. If Groq is rate limiting, `LLMBusy` surfaces as a 503 with a retry message — that is
   the intended behaviour, not a bug. Lower `LLM_MAX_CONCURRENCY` if we are the cause, or
   raise the Groq limit.

### "A student lost a token but got no interview"

Reservations are released automatically below the consume threshold (3 answers or
5 minutes). To check one session:

```sql
SELECT kind, amount, reason, created_at FROM token_ledger
WHERE session_id = '<session-id>' ORDER BY created_at;
```

A `reserve` with no `consume` or `release` means the session is still open. Ending it
settles the token. To refund by hand, use the admin grant endpoint with a reason — never
an UPDATE, so the ledger stays the truth.

### "A payment went through but no tokens arrived"

Tokens are granted **only** by the webhook.

1. `SELECT order_id, payment_id, status, tokens_granted, signature_verified FROM payments WHERE order_id = '...';`
2. `status = 'created'` means no webhook arrived. Check the Razorpay dashboard's webhook
   delivery log and that `RAZORPAY_WEBHOOK_SECRET` matches.
3. Razorpay retries failed deliveries; the grant is idempotent on `payment_id`, so a
   replay is safe.

### "Reports are stuck on processing"

Evaluation is a queued job, not a background task.

```sql
SELECT id, kind, status, attempts, last_error FROM job_queue
WHERE status IN ('queued','running','failed','dead') ORDER BY created_at DESC LIMIT 20;
```

`POST /api/super/jobs/drain` (admin) runs the queue by hand. `status = 'dead'` means it
exhausted its attempts — `last_error` says why.

### "Redis is down"

Live interview state is lost but **sessions are recoverable**: the next turn rebuilds
state from Postgres (`session_service.rebuild_from_db`). Rate limiting fails open and
logs `ratelimit_backend_unavailable`.

---

## Things deliberately left unfinished

- **OTP delivery.** No email or SMS provider is wired up. Locally the code goes to the
  log; in production the endpoint returns 503 rather than claiming it sent something.
  Pick a provider (Resend for email; MSG91 / Gupshup / Twilio for SMS, which needs Indian
  DLT template registration) and complete `app/services/notify.py` and `app/integrations/{email,sms}.py`.
- **TOTP enrolment.** Verification works and admins without it are flagged in the logs,
  but there is no enrolment endpoint yet.
- **Frontend.** Still calls the old WebSocket and credit endpoints. The backend contract
  changed; see PLAN.md Phase 1c.

---

## Free tier: what is constrained, and the upgrade swap list

Everything plan-specific is an environment variable. Moving to paid plans is an env
change and a redeploy — no code change, nothing to rebuild.

### Current posture

| Service | Plan | What it costs us |
|---|---|---|
| Supabase | Free | Project pauses after ~7 days idle; no on-demand backup; no PITR |
| Vercel | Hobby | Cron fires **once a day**, max **2 jobs** per project; short function timeout |
| Groq | Free key | Very low concurrency and requests/minute |
| Razorpay | Test mode | No real money moves |
| SMS (MSG91) | Off | Phone OTP returns 503; DLT template not approved |
| Upstash | Free | Request cap per day |

### How each constraint is handled

- **Supabase pausing.** A daily `GET /api/cron/keepalive` reads from Postgres and pings
  Redis. It deliberately does real work: a 200 that never reached Postgres would keep the
  cron green while the project paused anyway.
- **Daily cron, 2 jobs.** The two slots are `keepalive` and `maintenance`. `maintenance`
  does *all* the periodic work — drains the job queue, expires token lots, purges
  snapshots — because there is no third slot for `drain-jobs`.
- **A daily drain would leave reports pending for up to 24 hours.** So a report that is
  still `processing` drains its own evaluation job inline (`OPPORTUNISTIC_DRAIN=true`),
  claiming the row the same way the worker does so a concurrent cron cannot double-run
  it. Reports appear in seconds instead.
- **Groq concurrency.** `LLM_MAX_CONCURRENCY=2` and `RATE_LIMIT_LLM=12/60`. When the key
  is saturated the turn endpoint returns **503 with a readable message** and the browser
  retries with backoff, then offers a Retry button. It never invents a question to paper
  over it, and a turn that hit backpressure is not half-written, so the retry is clean.
- **Supabase pooler connections.** `DB_POOL_SIZE=1`, `DB_MAX_OVERFLOW=2`, because many
  short-lived functions share a small Free-tier connection allowance.

### Swap list — Free to paid

| Upgrade | Change | Effect |
|---|---|---|
| **Supabase Pro** | `DB_POOL_SIZE=5`, `DB_MAX_OVERFLOW=10` | More concurrent interviews. Also turn on PITR; the dashboard gains on-demand backups, so the manual `pg_dump` becomes a belt rather than the only snapshot. |
| | Remove the `keepalive` cron | Pro projects do not pause. Frees a cron slot if you stay on Hobby. |
| **Vercel Pro** | Add `{ "path": "/api/cron/drain-jobs", "schedule": "*/5 * * * *" }` to `vercel.json`; set `CRON_DRAIN_BATCH=5` | Queue drains every 5 minutes. |
| | `OPPORTUNISTIC_DRAIN=false` | The inline drain becomes unnecessary work on a GET once cron keeps up. |
| **Groq paid** | `LLM_MAX_CONCURRENCY=8..16`, `RATE_LIMIT_LLM=60/60` | Raise until p95 turn latency degrades, not beyond. Watch `llm_usage` for `ok=false`. |
| | `LLM_MODEL_QUESTION` / `LLM_MODEL_GRADER` | Swap models without a deploy. Benchmark grading agreement first (PLAN.md §10). |
| | `LLM_PRICING_JSON` | Keeps cost-per-interview accurate after a price change. The §9 guardrail depends on it. |
| **Razorpay live** | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | Startup **refuses** a `rzp_test_` key when `ENV=production`, so a half-switched deploy fails loudly. Do a full test-mode pass first. |
| **SMS live** | `SMS_ENABLED=true`, `SMS_API_KEY`, `MSG91_TEMPLATE_ID`, `MSG91_SENDER_ID` | Only after DLT approval. The template variable must be named `otp`. `/api/super/config-check` reports which state it is in. |
| **Upstash paid** | No change | Same REST URL and token. |
| **Email** | `RESEND_API_KEY` (starts `re_`) | Until this is set, email OTP returns 503 in production rather than claiming a code was sent. |

Nothing in that table touches application code. If an upgrade ever seems to need a code
change, that is a bug in how the value is wired, not a plan limitation.

### What the two cron jobs are for

`vercel.json` is strict JSON and rejects unknown properties, so the reasoning lives here
rather than beside the schedules.

| Path | Schedule | Why |
|---|---|---|
| `/api/cron/keepalive` | `0 6 * * *` | Reads from Postgres and pings Redis so a Free-tier Supabase project does not pause after ~7 days idle. An auto-paused project looks exactly like an outage to a student mid-interview. It does real work on purpose: a 200 that never reached Postgres would keep the cron green while the project paused anyway. |
| `/api/cron/maintenance` | `17 3 * * *` | Drains the job queue (evaluations, JD matches), expires token lots, purges proctoring snapshots past 14 days. |

**Vercel Hobby allows two cron jobs per project, firing once a day.** Both slots are
taken, which is why `maintenance` does all the periodic work instead of `drain-jobs`
having its own slot — and why a pending report drains its own evaluation inline
(`OPPORTUNISTIC_DRAIN=true`), rather than waiting up to 24 hours for the next run.

On Vercel Pro, add a third entry and reduce the inline work:

```json
{ "path": "/api/cron/drain-jobs", "schedule": "*/5 * * * *" }
```

then set `CRON_DRAIN_BATCH=5` and `OPPORTUNISTIC_DRAIN=false`.

Both endpoints require `Authorization: Bearer $CRON_SECRET`. Vercel sends that header
automatically when `CRON_SECRET` is set as a project environment variable. Without it the
endpoints return 503 and refuse to run — failing closed, because an unauthenticated
maintenance endpoint is worse than no cron.
