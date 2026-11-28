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

## Applying migrations

**Always snapshot first.** The project holds real data: 9 users, 67 interview sessions,
173 transcripts, 83 credit transactions.

```bash
# 1. Snapshot in the Supabase dashboard (Database → Backups), note the time.
# 2. Review what will run:
alembic upgrade 0002_phase1_foundation --sql | less
# 3. The existing schema was never migrated, so record it as the baseline:
alembic stamp 0001_baseline
# 4. Apply:
alembic upgrade head
# 5. Verify:
alembic current          # expect 0003_backfill_tokens
```

Notes:

- `0001_baseline` must be **stamped**, not run, on the existing project. It only exists so
  a fresh environment can be built from migrations.
- `0002` is additive except `student_credits.updated_at` TEXT → TIMESTAMP (approved).
- `0003` moves data (credits → token ledger) and refuses `--sql` mode; it needs a live
  connection.
- `student_credits` and `credit_transactions` are **kept** as the historical record.
  Nothing reads them after `0003`.

Rollback: `alembic downgrade 0001_baseline` undoes `0002` and `0003`.
`0001` refuses to downgrade on purpose — it would drop every interview on the platform.

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
