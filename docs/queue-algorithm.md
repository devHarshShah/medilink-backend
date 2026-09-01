# MediLink Queue Algorithm

End-to-end reference for the cross-hospital queue: how a patient is assigned to a
doctor, how they are re-routed while waiting, when they are told to leave, and how
every edge case is handled.

Code lives in:

| File | Responsibility |
|---|---|
| `src/services/queue/scoring.py` | Pure maths — distance, wait, score, lock-in, hysteresis, severity helpers. No I/O. |
| `src/services/queue/engine.py` | All DB work — candidate building, assignment, re-scoring. |
| `src/services/queue/rescorer.py` | The 30-second background loop. |
| `src/api/queue.py` | HTTP routes + status lifecycle. |
| `src/models/queue.py`, `src/models/enums.py` | `QueueEntry` table and enums. |
| `src/services/triage/` | Symptom → specialisation + urgency (Claude, with a rule-engine fallback). |

---

## 1. Product model

A triaged patient waits **at home**, not in a corridor, and is told **when to
leave** for whichever nearby centre will actually see them soonest. The number
that matters is therefore not "how long is the queue" but "when am I seen,
including travel" — a busy hospital 5 minutes away can beat a quiet one an hour
away.

Two queue types (`QueueType`):

| Type | Meaning |
|---|---|
| `hospital_specific` | The patient chose one hospital. That choice is a promise: the entry is pinned there and never floats. |
| `cross_hospital` | The patient named candidate doctors and let the server choose. The entry can be re-assigned to a better `(doctor × hospital)` pair until lock-in. |

---

## 2. Data model

### `QueueEntry` (`src/models/queue.py`, table `queue_entries`)

| Column | Notes |
|---|---|
| `patient_id`, `doctor_id`, `hospital_id` | The current assignment. `doctor_id`/`hospital_id` change on a re-assignment. |
| `queue_type` | `hospital_specific` / `cross_hospital` (see §1). |
| `queue_number` | Human token, `max(queue_number)+1` per `(doctor, hospital, day)` — restarts daily, not globally unique. Re-assignment gets a fresh number at the new centre. |
| `status` | `QueueStatus` — see §10. Indexed. |
| `priority` | `QueuePriority`, default `normal`. Drives call order (§7). |
| `is_emergency` | Convenience mirror of `priority == emergency`. |
| `symptoms_description`, `ai_summary`, `ai_recommendation` | Free text + the full triage `SymptomsAnalysisResponse` (JSON). `ai_recommendation` is re-read by the re-scorer, so its specialisation weights and urgency persist. |
| `candidate_doctor_ids` | The doctors the patient offered at join time. **The re-scorer may only move between these.** |
| `patient_latitude` / `patient_longitude` | From the browser. Optional — everything degrades gracefully without them (§11). |
| `joined_at` | Server timestamp; ties in call order break on this (FIFO within a priority). |
| `consultation_started_at` / `consultation_completed_at` / `patient_notified_at` | Set on status transitions. The consultation duration feeds the wait estimate EMA (§7). |
| `locked_in_at` | Set once the patient should be travelling. After this the entry **stops floating** (§9). |
| `leave_at` | When to set off, recomputed on every scoring pass. |
| `reassignment_count` | How many times the re-scorer has moved this entry. |

### Enums (`src/models/enums.py`)

```
QueueStatus  : waiting → in_consultation → completed   (+ cancelled, no_show)
QueueType    : hospital_specific | cross_hospital
QueuePriority: low(0)  normal(1)  high(2)  emergency(3)   — .weight is the number
```

---

## 3. Triage input

`POST /api/ai/analyze-symptoms` (`src/api/ai.py`) maps free text to a
`SymptomsAnalysisResponse` (`src/schemas/ai.py`):

| Field | Use in the queue |
|---|---|
| `recommended_specializations` | List of `{specialization, priority (1–3, 1 = best), reason}`. `priority` becomes a per-specialisation weight: `weight = (4 − priority) / 3` → `1.0 / 0.67 / 0.33`. Unlisted specialisations score `0.6`. |
| `urgency_level` | `emergency` / `high` / `medium` / `low`. Drives **queue priority** (§4) and the **severity lock** (§6). |
| `red_flags` | Human-readable reasons the rule engine escalated to `emergency` (e.g. "Chest pain — possible cardiac event"). |
| `summary` | Stored as `ai_summary`, shown to patient and doctor. |

Provider selection (`src/services/triage/`): Claude if `ANTHROPIC_API_KEY` is set
and responsive; otherwise a deterministic keyword rule engine
(`src/services/triage/rules.py`). The queue never blocks on the LLM — every path
degrades to rules. Both are constrained to the **11 staffed specialisations**
(`src/services/triage/base.py::SPECIALIZATIONS`); `General Medicine` is the
explicit catch-all / fallback.

Triage is optional. If the patient skips it, `ai_recommendation` is `null` and the
engine uses plain distance/wait ranking with no severity lock.

---

## 4. Joining a queue

`POST /api/queue/join` → `engine.join_queue()` (`src/services/queue/engine.py`).

Request (`JoinQueueRequest`): `doctor_ids[]` (the patient's picks), optional
`hospital_id` (required for `hospital_specific`), `symptoms_description`,
`ai_recommendation`, `priority`, `queue_type`, optional `patient_location`.

Steps:

1. **One active entry per patient.** If the patient already has a `waiting` or
   `in_consultation` entry → soft failure `"You are already in a queue…"`.
   Without this a patient silently holds slots across several doctors and inflates
   every queue they touch.
2. **`match_scores_from_ai(ai_recommendation)`** → `{specialisation: weight}`.
3. **`select_candidates(...)`** (§5, §6) → `(candidates, required_specialisation)`.
   Empty list → soft failure:
   - severity-locked: `"No {spec} is available right now. If this is an emergency,
     go to the nearest emergency department immediately."`
   - otherwise: `"None of the selected doctors are currently available."`
4. **`rank_candidates(candidates, lat, lng)`** → best (lowest score) first (§5).
5. **Priority override from triage.** `urgency == "emergency"` → force
   `EMERGENCY`; `urgency == "high"` and the client sent something lower → bump to
   `HIGH`. A client that posts `normal` for a described stroke is not believed.
6. **Create the entry** on the winning pair: fresh `queue_number`,
   `leave_at = compute_leave_at(...)`, and if the patient should basically already
   be travelling, set `locked_in_at` immediately.
7. **Respond** (`JoinQueueResponse`) with the assignment plus `alternative_options`
   = `ranked[1:4]` (the runners-up, so the wizard can show "or you could pick…").

### Where a doctor "is" — `doctor_locations()`

A doctor consults at different hospitals across a week. The source of truth is an
**open `DoctorCheckIn`** with `presence_status ∈ {available, busy}` — `break` /
`offline` check-ins are present but excluded. Doctors who have **never** checked in
anywhere fall back to `Doctor.hospital_id`, so a freshly seeded DB and pre-adoption
usage both work.

---

## 5. Scoring (`src/services/queue/scoring.py`)

For each candidate `(doctor × hospital)`:

**Travel** — `travel_minutes()`:
```
distance   = haversine(patient, hospital)          # great-circle km
road_km    = distance × 1.3                        # urban detour factor
travel_min = road_km / queue_travel_speed_kmh × 60 # default 25 km/h
```
Missing coordinates at either end → `None` ("unknown"), and the scorer substitutes
`UNKNOWN_TRAVEL_MINUTES = 30`. Never `0` — an unlocatable hospital would otherwise
look infinitely close and win everything.

**Wait** — `waiting_minutes()`:
```
wait = queue_size × max(avg_consultation_minutes, 1)
```
`queue_size` = patients currently `waiting` for that `(doctor, hospital)`.
`avg_consultation_minutes` is a live per-doctor EMA (§7), seeded at 12.

**Score** (lower = better):
```
score = max(wait, effective_travel)                 # when you're actually seen
      + TRAVEL_WEIGHT × effective_travel             # TRAVEL_WEIGHT = 1.0
      + MATCH_PENALTY_MINUTES × (1 − match_score)    # MATCH_PENALTY_MINUTES = 25
```

| Term | Why |
|---|---|
| `max(wait, travel)` | You're seen at whichever finishes last. Arriving early still means sitting through the queue. |
| `+ 1.0 × travel` | Time in a car is worse than time at home, charged again on top. Net: a farther centre only wins if the wait it saves **exceeds the extra driving**. Below weight 1.0 the engine would ship people across the city to save 2 minutes. |
| `+ 25 × (1 − match_score)` | A slightly faster but clinically worse-matched doctor shouldn't win on a rounding difference. Perfect match adds 0; the `0.6` default adds 10 min. **This is a soft preference — §6 makes it hard for urgent cases.** |

`rank_candidates()` sorts by `(score, doctor_id)` — the `doctor_id` tie-break makes
ranking deterministic.

---

## 6. Severity lock

**Problem:** the §5 match penalty is soft. A `high`-urgency cardiac patient can
still be routed to a faster General Medicine doctor if the wait saving exceeds
~25 effective minutes. For urgent cases that is wrong.

**Rule** (`engine.select_candidates` + `scoring.py` helpers):

- **Triggers when** `urgency_level ∈ {emergency, high}`
  (`SEVERITY_LOCK_URGENCIES`). `medium` / `low` keep the soft-penalty behaviour.
- **"Matching" = the priority-1 specialisation only** (`top_specialization()`) —
  the single best recommendation. 2nd/3rd suggestions and the `General Medicine`
  fallback do **not** count.
- **Candidate resolution:**
  1. Filter the patient's own picks to that specialisation.
  2. If none match → **auto-expand network-wide**: every available doctor of that
     specialisation across all hospitals (`build_specialist_candidates()` →
     `doctor_locations(db, None)` filtered by specialisation).
  3. Still none → return `[]`. `join_queue` turns this into the emergency soft
     error; the re-scorer leaves the entry where it is (never downgrades it to a
     generalist).
- **In the re-scorer:** applied identically. `emergency` entries never float
  anyway (§8), so in practice this shapes `high`-urgency floats — a `high` patient
  on a generalist is pulled to a specialist when one frees up, and never drifts
  the other way.
- **Interaction with priority:** independent. The severity lock picks *which
  doctor*; the priority override (§4 step 5) sets *where in that doctor's queue*
  the patient sits.

No triage result, or triage named no specialisation → no lock, ordinary ranking.

---

## 7. Position and wait shown to the patient

**Call order** — `position_in_queue()`: order a doctor's `waiting` list by
**`priority.weight` descending, then `joined_at` ascending**. Higher priority jumps
the line; within a priority it is first-come-first-served. `in_consultation` = 0.

**Wait estimate** — `estimate_wait()`:
```
wait_minutes = (position − 1) × doctor.avg_consultation_minutes
```
plus the travel estimate. Returned by `GET /api/queue/status/{id}` and the
doctor's working list.

**The EMA** — `_update_average_consultation_time()` (`src/api/queue.py`), run when
a consultation is marked `completed`:
```
avg = round(0.8 × avg + 0.2 × actual_minutes, 2)
```
Durations outside `1–120 min` are ignored (a misclick or forgotten "complete"
shouldn't teach the model that consultations take eight hours). Recent
consultations matter more; one outlier can't swing the whole queue's predictions.

---

## 8. Floating queue / the re-scorer

`src/services/queue/rescorer.py` runs one asyncio task for the app's lifetime
(started in `src/main.py` lifespan):

```
every queue_rescore_interval_seconds (default 30):
    rescore_all_waiting(db)
```

**`rescore_all_waiting()`** selects entries where
`status = waiting AND queue_type = cross_hospital AND locked_in_at IS NULL` — this
exact query is the one that appears every 30 s in the logs. On an idle DB it
matches nothing, does nothing, and the read-only session closes with a normal
`ROLLBACK` (**not an error** — a real failure logs `WARNING Queue re-scoring pass
failed`). Each entry is processed in its own `try/except` so one bad row can't kill
the sweep.

**`rescore_entry(entry)`** for each:

1. **Guards** — skip unless `status == waiting`, not `locked_in`, **not
   `EMERGENCY`** (continuity beats optimisation), and `queue_type ==
   cross_hospital` (a `hospital_specific` join is a promise).
2. **Recompute** candidates via `select_candidates(..., exclude_entry_id=entry.id)`
   — the `exclude` keeps the patient from being counted as waiting *ahead of
   themselves*, which would bias every comparison toward moving them. Empty result
   → `return False`.
3. **Lock-in first.** If `should_lock_in(current)` → set `locked_in_at` +
   `leave_at` and **stop**. Don't send a soon-to-travel patient somewhere new.
4. **Move only for a clear win** — `is_worth_moving`:
   ```
   (current_score − best_score) > queue_reassign_hysteresis_minutes   # 8 min
   ```
   Without this margin a patient oscillates between two near-equal centres on
   every pass. If the current doctor is unavailable anywhere, `current_score = ∞`
   and any option is an improvement.
5. **On a move:** reassign `doctor_id` / `hospital_id`, fresh `queue_number`,
   `reassignment_count += 1`, publish `EVENT_REASSIGNED` to the patient and
   `EVENT_POSITION_CHANGED` to **both** doctors' boards.
6. Recompute `leave_at`; lock in if now warranted; `commit`.

**Idempotent:** a second pass finds no improvement and moves nothing.

**Deployment note:** with more than one worker each runs its own copy of the loop.
The moves are idempotent so it is safe, but a production deployment should elect a
single runner. Today we run one worker.

---

## 9. Lock-in

`should_lock_in(wait, travel)`:
```
wait ≤ travel + queue_safety_buffer_minutes      # buffer default 10
```
Once the remaining wait is short enough that the patient needs to be *on the
road*, `locked_in_at` is set. Also set unconditionally when a doctor calls the
patient (`status → in_consultation`, `src/api/queue.py`).

After lock-in the entry is frozen to its centre — it is excluded from the
re-scorer sweep, and `rescore_entry` bails on it even if called directly. Being
re-routed mid-journey is worse than a slightly longer wait.

`compute_leave_at(wait, travel)`:
```
leave_at = now + max(0, wait − travel − safety_buffer)
```
Never in the past — if the turn is imminent, the honest answer is "leave now".

---

## 10. Status lifecycle

`PATCH /api/queue/status/{id}` (doctor only, own patient). Allowed transitions
(`ALLOWED_TRANSITIONS`, `src/api/queue.py`) — anything else → `409`:

| From | To | Side effects |
|---|---|---|
| `waiting` | `in_consultation` | `consultation_started_at`, `patient_notified_at`, force `locked_in_at`; `EVENT_CALLED`. |
| `waiting` | `no_show` / `cancelled` | `EVENT_CANCELLED` on cancel. |
| `in_consultation` | `completed` | `consultation_completed_at`; feed the EMA (§7); `EVENT_COMPLETED`. |
| `in_consultation` | `cancelled` | `EVENT_CANCELLED`. |
| `no_show` | `waiting` | re-queue a patient who turned up late. |
| `completed` / `cancelled` | — | terminal. |

Every transition also publishes `EVENT_POSITION_CHANGED` to the doctor's board —
everyone behind the patient just moved up.

**Patient cancel** — `DELETE /api/queue/cancel/{id}`: only the owning patient, only
while `waiting` (`409` once `in_consultation`).

A queue that could jump `completed → waiting` would corrupt every position
calculation behind it, hence the explicit whitelist.

---

## 11. Case matrix

| Situation | Behaviour | Where |
|---|---|---|
| No patient geolocation | `travel = None` → `UNKNOWN_TRAVEL_MINUTES (30)` in the score; `leave_at`/distance use the same default; ranking still works on wait. | `scoring.py::score_candidate`, `travel_minutes` |
| Hospital has no coordinates | Same — treated as unknown/30 min, **not** as distance 0, so it can't win by default. | `travel_minutes` |
| Doctor never checked in anywhere | Falls back to their primary `Doctor.hospital_id`. | `engine.doctor_locations` |
| Doctor on `break` / `offline` | Excluded from candidates (present but not taking patients). | `engine.doctor_locations` |
| None of the chosen doctors available | Soft failure `success:false`, `"None of the selected doctors are currently available."` | `engine.join_queue` |
| Patient already in a queue | Soft failure `"You are already in a queue…"`. | `engine.join_queue` |
| Triage skipped (`ai_recommendation` null) | Plain distance/wait ranking, `match_score` default `0.6`, no severity lock, no priority override. | `engine.join_queue` |
| Urgent (`emergency`/`high`), chosen doctors include a matching specialist | Restricted to those specialists, then ranked normally. | §6 |
| Urgent, chosen doctors have no matching specialist | Auto-expand to all matching specialists network-wide, ranked. | `engine.build_specialist_candidates` |
| Urgent, no matching specialist anywhere | `join`: soft failure naming the specialty + "go to the nearest emergency department". Re-scorer: entry left untouched. | §6 |
| Client sends low priority for an emergency description | Triage `urgency` overrides it to `EMERGENCY`/`HIGH`. | `engine.join_queue` |
| Doctor checks out while a patient waits | Next re-score pass finds the current pair gone → `current_score = ∞` → moved to the best remaining option. | `engine.rescore_entry` |
| Marginal improvement (< 8 min) | No move (hysteresis). | `scoring.is_worth_moving` |
| Turn imminent (`wait ≤ travel + buffer`) | Lock in where they are instead of moving; set `leave_at`. | `engine.rescore_entry`, `scoring.should_lock_in` |
| `hospital_specific` entry | Never floats — excluded from the sweep and bailed on in `rescore_entry`. | `engine.rescore_entry` |
| `emergency` priority entry | Never floats. | `engine.rescore_entry` |
| Bogus `avg_consultation_minutes = 0` | `waiting_minutes` clamps to `× max(avg, 1)`, so a deep queue doesn't look instant. | `scoring.waiting_minutes` |
| Re-score pass raises on one entry | Logged as `WARNING`, `rollback`, sweep continues with the next entry. | `engine.rescore_all_waiting` |
| Multiple app workers | Each runs the loop; moves are idempotent (safe), but elect one runner in prod. | `rescorer.py` |

---

## 12. Tuning knobs

`src/core/config.py` (env-overridable):

| Setting | Default | Effect |
|---|---|---|
| `queue_travel_speed_kmh` | 25 | Assumed city driving speed. |
| `queue_safety_buffer_minutes` | 10 | Arrive-early margin; also the lock-in threshold. |
| `queue_rescore_interval_seconds` | 30 | How often the floating sweep runs. |
| `queue_reassign_hysteresis_minutes` | 8 | Minimum score improvement to justify a move. |
| `queue_default_consultation_minutes` | 12 | Seed for a doctor's EMA. |

Code constants:

| Constant | File | Value | Meaning |
|---|---|---|---|
| `TRAVEL_WEIGHT` | `scoring.py` | 1.0 | Travel penalty vs. waiting. |
| `MATCH_PENALTY_MINUTES` | `scoring.py` | 25 | Soft cost of a poorer clinical match. |
| `UNKNOWN_TRAVEL_MINUTES` | `scoring.py` | 30 | Stand-in when coordinates are missing. |
| `SEVERITY_LOCK_URGENCIES` | `scoring.py` | `{emergency, high}` | Urgencies that force specialist-only assignment. |

---

## 13. Worked examples

### A. A generalist wins on speed (non-urgent)

Patient at home; triage `low`, top spec Cardiology (weight 1.0), General Medicine
listed 2nd (weight 0.67). Candidates:

| Doctor @ Hospital | queue | avg | dist | wait | travel | score |
|---|---|---|---|---|---|---|
| Cardiologist @ A | 4 | 20 | 3 km | 80 | ≈9.4 | `max(80,9.4) + 9.4 + 0 = 89.4` |
| Gen. Med @ A | 0 | 10 | 3 km | 0 | ≈9.4 | `max(0,9.4) + 9.4 + 25·0.33 ≈ 27` |

Winner: **Gen. Med @ A** — zero wait beats the 8-min match penalty. Not urgent, so
no severity lock. `leave_at ≈ now` and it locks in immediately (`0 ≤ 9.4 + 10`).

### B. Severity lock overrides that (urgent)

Same candidates, but triage now returns `urgency_level: "high"`. Severity lock
triggers, `top_specialization = "Cardiology"`. The Gen. Med option is filtered out
before ranking. Winner: **Cardiologist @ A**, wait ≈ 80 min, and `priority` is
bumped to `HIGH` so the patient sits above `normal` patients in that queue. If the
patient had picked *only* a generalist, `build_specialist_candidates` would have
pulled in every available Cardiologist network-wide instead.

### C. Float, then lock-in, over time

Patient joins pointed at Cardiologist @ A: 6 ahead, avg 20 → wait 120 min, travel
20 → `leave_at = now + 90`, not locked (`120 > 30`). A quieter Cardiologist @ B
(1 ahead, wait 20, travel 37) exists.
- **t = 0:** score A = `max(120,20) + 20 = 140`; score B = `max(20,37) + 37 = 74`.
  `140 − 74 = 66 > 8` → **move to B**, fresh token, `reassignment_count = 1`.
- **t later:** B's queue drains to 0, wait 0, travel 37 → `0 ≤ 37 + 10` →
  **lock in at B**, `leave_at = now`. From here the entry no longer floats.
- Doctor calls the patient → `status = in_consultation`, `locked_in_at` already
  set, `consultation_started_at` stamped.

---

## 14. Endpoint reference (`/api/queue/*`)

| Method + path | Role | Purpose |
|---|---|---|
| `POST /join` | patient | Assign the best `(doctor × hospital)` from the candidates; returns the entry + runners-up. Soft-fails with `success:false` + `error`. |
| `GET /my-queues` | patient | The patient's entries (envelope `{queues: [...]}`). |
| `GET /status/{queue_id}` | patient (own) / doctor (own) / admin / nurse | Live status: position, wait, `leave_at`, travel, `locked_in`. |
| `DELETE /cancel/{queue_id}` | patient (own) | Cancel while `waiting`. |
| `GET /hospitals` | any auth | Active hospitals for the picker. |
| `GET /available-doctors` | any auth | Doctors the patient can choose, ranked by the same engine. Params: `queue_type`, `hospital_id`, `specialization(s)`, `include_general`, `latitude`, `longitude`. |
| `GET /doctor/{doctor_id}` | doctor (own) / admin / nurse | The doctor's working list, ordered priority-then-arrival. |
| `PATCH /status/{queue_id}` | doctor (own patient) | Status transition (§10). |

Background: `rescorer._loop` (no route) runs `rescore_all_waiting` every 30 s.
SSE events are delivered via `/api/events/*` (`src/api/events.py`).
