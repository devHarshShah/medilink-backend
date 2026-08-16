# MediLink Backend

FastAPI backend for MediLink — a cross-hospital healthcare platform where a
triaged patient waits at home in a virtual queue and is told when to leave for
whichever nearby centre will actually see them soonest.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Auth, patients, admin, doctor records, pharmacy dispensing, QR access | **done** |
| 2 | AI symptom triage (`/api/ai/*`) | **done** |
| 3 | Cross-hospital queue + SSE (`/api/queue/*`) | **done** |
| 4 | Doctor check-in, schedules, stats | **done** |
| 5 | Consent, document vault, consultation chart | planned |
| 6 | Diagnostic centre / lab loop | planned |

## Quick start

```bash
cp .env.example .env
docker compose up -d              # postgres 18, redis 7, minio, api
docker compose exec api python -m src.scripts.seed
```

API on http://localhost:8000 · docs at `/docs` · MinIO console on http://localhost:9001.

Then run the frontend against it:

```bash
cd ../medilink-frontend && cp .env.example .env.local && pnpm install && pnpm dev
```

### Without Docker

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
docker compose up -d postgres redis minio minio-init   # infra only
.venv/bin/alembic upgrade head
.venv/bin/python -m src.scripts.seed
.venv/bin/uvicorn src.main:app --reload
```

## Demo accounts

All seeded accounts use the password `MediLink@123`.

| Role | Email |
|---|---|
| Admin | `admin@medilink.health` |
| Doctor | `ananya.rao@medilink.health` |
| Patient | `harsh.kumar@example.com` (UHID `ML2500000001`) |
| Patient (with history) | `ravi.subramanian@example.com` (UHID `ML2500000003`) |
| Pharmacy | `pharmacy@medilink.health` |
| Diagnostic centre | `lab@medilink.health` |

## AI triage

`POST /api/ai/analyze-symptoms` maps a free-text symptom description to
departments and an urgency level. It is routing, not diagnosis.

Two providers sit behind one interface:

- **Claude** (`claude-opus-5` by default, set `TRIAGE_MODEL` to change) using
  structured outputs, so the response is schema-validated rather than parsed
  from prose.
- **A deterministic rule engine** — keyword and severity heuristics with an
  explicit red-flag list.

The rule engine runs whenever Claude is absent, slow, rate-limited, refused, or
returns something unusable, and the response is marked `fallback_used: true`.
**Triage never fails a request** — a 500 here would block the entire booking
flow, so every failure path degrades instead of raising.

Without `ANTHROPIC_API_KEY` the service runs rule-only and `/api/ai/health`
reports `degraded` (not unhealthy — triage still works).

## The cross-hospital queue

The core of the product. A patient names candidate doctors; the server picks the
(doctor x hospital) pair that will actually see them soonest and tells them when
to leave, so they wait at home instead of in a corridor.

**Scoring** (`src/services/queue/scoring.py`, pure functions, no I/O):

```
wait   = people ahead x the doctor's rolling average consultation length
travel = haversine distance / assumed speed, with an urban detour factor
score  = max(wait, travel) + travel + a penalty for a poorer clinical match
leave_at = now + max(0, wait - travel - safety buffer)
```

Travel is charged on top of the time it displaces, so a centre only wins on
distance if the time it saves exceeds the extra journey. A hospital with no
coordinates is treated as *unknown* distance, never as zero.

**Floating re-assignment.** A background task (`rescorer.py`) re-scores every
waiting cross-hospital entry every 30s and moves patients to a better centre
while they are still at home. It stops at four boundaries:

- **Lock-in** — once `wait <= travel + buffer` the slot freezes. Being
  re-routed mid-journey is worse than a slightly longer wait.
- **Hysteresis** — a move needs to beat the current option by a clear margin,
  or a patient oscillates between two near-equal centres on every pass.
- **Emergencies** are never moved; continuity beats optimisation.
- **Hospital-specific joins** are never moved; picking one centre is a promise.

Patients are told when they are moved rather than finding out on arrival.

**Live updates.** Redis pub/sub behind SSE at `/api/events/queue/{id}` and
`/api/events/doctor/{id}`. EventSource cannot set headers, so the frontend
exchanges its bearer token for a short-lived stream token
(`POST /api/events/token`) that rides in the query string — a separate token
type that is useless as a bearer credential, because query strings land in
access logs. The frontend keeps polling as a fallback, slowed to 2 minutes
while the stream is healthy.

**Doctor availability is a live fact.** The queue reads open check-ins, not a
doctor's profile: a doctor consults at several hospitals across a week, and
sending patients to a building they left this morning is the failure check-in
exists to prevent. A doctor on a break is present but attracts no new patients.
Doctors who have never used check-in fall back to their primary hospital, so
the queue works before anyone adopts it. Checking out re-scores everyone who
was waiting for you.

> **Single worker.** The re-scorer runs one task per process. Running multiple
> uvicorn workers would run several copies — the moves are idempotent, but a
> multi-worker deployment should elect a single runner.

## Tests

```bash
.venv/bin/python -m pytest              # unit + API tests (SQLite, no infra needed)
.venv/bin/python -m src.scripts.smoke_test   # end-to-end against a running API
```

## Why it looks the way it does

The original backend was lost. It was reconstructed from two artifacts in the
surviving frontend, and several choices here exist purely to keep faith with them:

- **`medilink-frontend/openapi.json`** is a real dump of the lost service. All 41
  of its endpoints are reproduced. `src/schemas/healthcare.py` keeps its module
  name because the spec's `src__schemas__healthcare__UserResponse` `$ref` — and
  therefore the frontend's generated types — depends on it.
- **`medilink-frontend/src/lib/api-client.ts`** already contains finished clients
  for `/api/queue/*`, `/api/ai/*`, `/api/consultation/*` and doctor check-in,
  none of which are in the dump. Those define the contract for phases 2–6.

Contract details that are load-bearing and easy to break:

- Errors must be `{"detail": ...}` — the frontend's axios interceptor reads
  exactly that, string or `[{loc,msg,type}]`.
- Login takes a **JSON body**, not OAuth2 form encoding.
- CORS runs with `allow_credentials=False`; the frontend authenticates by bearer header.
- `QRAccessResponse.qr_code` is **base64 PNG with no data-URI prefix** — the UI adds it.
- `duration_hours` accepts floats (the prescriptions screen sends `0.5`).
- Consent and document statuses are **uppercase** on the wire.

Regenerate the spec after changing routes so the frontend types can be re-derived:

```bash
.venv/bin/python -c "import json;from src.main import app;print(json.dumps(app.openapi(),indent=1))" > openapi.json
```
