# MediLink Backend

FastAPI backend for MediLink — a cross-hospital healthcare platform where a
triaged patient waits at home in a virtual queue and is told when to leave for
whichever nearby centre will actually see them soonest.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Auth, patients, admin, doctor records, pharmacy dispensing, QR access | **done** |
| 2 | AI symptom triage (`/api/ai/*`) | planned |
| 3 | Cross-hospital queue + SSE (`/api/queue/*`) | planned |
| 4 | Doctor check-in, schedules, stats | planned |
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
