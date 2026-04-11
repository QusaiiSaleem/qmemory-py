# Railway Worker Service Setup Runbook

The worker service must be created manually in the Railway dashboard
because the Railway CLI does not support setting a custom start
command at service creation time.

## Why a separate service?

The worker loop runs LLM-budgeted jobs (linker, dedup, reflector) once per hour
against every active user database in `qmemory.admin`. Running it inside the
API container would compete with HTTP requests for memory + CPU. A dedicated
service is cheap (~$5/month at the smallest Railway tier) and isolates failures.

## Prerequisites

- The main `qmemory` API service is already deployed and green.
- The `surrealdb` service is reachable via the internal network.
- Phase 3 migration is complete: `qusai` exists in `qmemory.admin.user` and
  `user_qusai` database contains ~8,871 memories.

## Steps

1. Open the Railway dashboard for the `qmemory` project.

2. Click **+ Create** → **Empty Service**.

3. Rename the new service to `qmemory-worker` (Settings → Rename Service).

4. Settings → Source:
   - **Repo**: `QusaiiSaleem/qmemory-py`
   - **Branch**: `main`
   - **Trigger**: auto-deploy on push (default)

5. Settings → Deploy:
   - **Build Command**: (leave empty — uses the existing Dockerfile)
   - **Start Command**:
     ```
     qmemory worker --interval 3600 --all-users
     ```
   - **Health Check Path**: (leave empty — worker is not an HTTP server)
   - **Restart Policy**: `ON_FAILURE`, max retries 3

6. Settings → Variables — copy these from the `qmemory` service:
   ```
   QMEMORY_SURREAL_URL=ws://surrealdb.railway.internal:8000
   QMEMORY_SURREAL_USER=root
   QMEMORY_SURREAL_PASS=${{surrealdb.SURREAL_PASS}}
   QMEMORY_SURREAL_NS=qmemory
   QMEMORY_SURREAL_DB=admin
   ANTHROPIC_API_KEY=...
   VOYAGE_API_KEY=...
   ```
   Note: `QMEMORY_SURREAL_DB=admin` — the worker starts against the admin DB
   so `_iter_active_user_dbs()` can list users. It then switches DB per user
   via the `_user_db` ContextVar inside the loop.

7. Resource limits (Settings → Resources):
   - Memory: 512 MB
   - CPU: 0.5 vCPU

8. Click **Deploy**. First build takes 5–10 minutes (Docker rebuild).

## Verification (after first deploy)

```bash
railway logs --service qmemory-worker --lines 100 --json | tail -30
```

Expected entries:
- `worker.started interval=3600s once=False all_users=True`
- `worker.user_cycle_start cycle=1 user=qusai db=user_qusai`
- `worker.linker cycle=1 result=...`
- `worker.dedup cycle=1 result=...`
- `worker.decay cycle=1 result=...`
- `worker.linter cycle=1 findings=N`
- `worker.cycle_done cycle=1 elapsed_ms=... findings=N`
- `worker.cycle_summary cycle=1 users_processed=1`

## Health report check via MCP tool

From Claude.ai or Claude Code, after wiring up your personal URL:

```bash
curl -s -X POST https://mem0.qusai.org/mcp/u/qusai/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"qmemory_health","arguments":{}}}'
```

Expected: JSON response with a recent `generated_at` timestamp. If it says
`"status": "no_report"`, the worker hasn't run yet — wait up to 1 hour.

## Rollback

If the worker misbehaves:

1. **Temporary stop**: Railway dashboard → `qmemory-worker` → **Settings** →
   change Start Command to `sleep infinity` → redeploy. Container stays up
   but does nothing. No data impact.
2. **Full removal**: Railway dashboard → `qmemory-worker` → **Settings** →
   **Danger Zone** → **Delete Service**. No data impact.

In both cases, nothing in any user database is touched. The worker only
reads/writes within each user's own DB (soft-deletes, edge creations,
salience updates, health reports).
