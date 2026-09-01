"""Queue assignment and re-assignment.

Everything that touches the database on behalf of the queue lives here so the
routers stay thin and the scoring stays pure.
"""

import logging
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.doctor_ops import DoctorCheckIn
from src.models.enums import DoctorAvailabilityStatus, QueuePriority, QueueStatus, QueueType
from src.models.organizations import Hospital
from src.models.people import Doctor, Patient
from src.models.queue import QueueEntry
from src.models.user import User
from src.services.events import (
    EVENT_POSITION_CHANGED,
    EVENT_REASSIGNED,
    publish_doctor_event,
    publish_queue_event,
)
from src.services.queue.scoring import (
    Candidate,
    ScoredCandidate,
    compute_leave_at,
    is_severity_locked,
    is_worth_moving,
    rank_candidates,
    score_candidate,
    should_lock_in,
    top_specialization,
    travel_minutes,
)

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (QueueStatus.WAITING, QueueStatus.IN_CONSULTATION)


async def doctor_locations(db: AsyncSession, doctor_ids: list[int] | None = None) -> list[tuple]:
    """Where each doctor can currently be consulted, as (Doctor, Hospital, User).

    An open check-in is the truth: a doctor consults at several hospitals across
    a week, and sending patients to a building they left this morning is the
    failure this whole feature exists to prevent.

    Doctors who have never checked in anywhere fall back to their primary
    hospital, so the queue still works before anyone adopts check-in, and so a
    freshly seeded database is usable.
    """
    base = (
        select(Doctor, Hospital, User)
        .join(User, Doctor.user_id == User.id)
        .where(Doctor.is_active.is_(True), User.is_active.is_(True), Hospital.is_active.is_(True))
    )
    if doctor_ids:
        base = base.where(Doctor.id.in_(doctor_ids))

    checked_in = await db.execute(
        base.join(DoctorCheckIn, DoctorCheckIn.doctor_id == Doctor.id)
        .join(Hospital, DoctorCheckIn.hospital_id == Hospital.id)
        .where(
            DoctorCheckIn.check_out_time.is_(None),
            # A doctor on a break or offline is present but not taking new
            # patients, so they must not attract queue assignments.
            DoctorCheckIn.presence_status.in_(
                [DoctorAvailabilityStatus.AVAILABLE, DoctorAvailabilityStatus.BUSY]
            ),
        )
    )
    rows = list(checked_in.all())
    located = {doctor.id for doctor, _, _ in rows}

    ever_checked_in = set((await db.execute(select(DoctorCheckIn.doctor_id).distinct())).scalars().all())

    fallback = await db.execute(
        base.join(Hospital, Doctor.hospital_id == Hospital.id).where(
            Doctor.id.notin_(located | ever_checked_in) if (located | ever_checked_in) else True
        )
    )
    rows.extend(fallback.all())
    return rows


async def queue_size_for(db: AsyncSession, doctor_id: int, hospital_id: int) -> int:
    """Patients still waiting for this doctor at this hospital."""
    result = await db.execute(
        select(func.count(QueueEntry.id)).where(
            QueueEntry.doctor_id == doctor_id,
            QueueEntry.hospital_id == hospital_id,
            QueueEntry.status == QueueStatus.WAITING,
        )
    )
    return int(result.scalar_one())


async def queue_sizes(db: AsyncSession, exclude_entry_id: int | None = None) -> dict[tuple[int, int], int]:
    """All waiting counts in one query, keyed by (doctor_id, hospital_id).

    Scoring N candidates would otherwise be N round trips.

    `exclude_entry_id` leaves one entry out. When re-scoring an existing entry
    it is already sitting in its current queue, and counting a patient as
    waiting ahead of themselves overstates their current option by a full
    consultation -- which quietly biases every comparison toward moving them.
    """
    stmt = select(QueueEntry.doctor_id, QueueEntry.hospital_id, func.count(QueueEntry.id)).where(
        QueueEntry.status == QueueStatus.WAITING
    )
    if exclude_entry_id is not None:
        stmt = stmt.where(QueueEntry.id != exclude_entry_id)
    result = await db.execute(stmt.group_by(QueueEntry.doctor_id, QueueEntry.hospital_id))
    return {(row[0], row[1]): int(row[2]) for row in result.all()}


def match_scores_from_ai(ai_recommendation: dict | None) -> dict[str, float]:
    """Turn the triage result into per-specialisation weights.

    Priority is 1-best; the frontend uses `4 - priority`, so we mirror that and
    normalise to 0..1 to keep the scoring units stable.
    """
    if not ai_recommendation:
        return {}
    scores: dict[str, float] = {}
    for item in ai_recommendation.get("recommended_specializations") or []:
        try:
            scores[item["specialization"]] = max(0.0, (4 - int(item["priority"])) / 3.0)
        except (KeyError, TypeError, ValueError):
            continue
    return scores


def _rows_to_candidates(
    rows: list[tuple],
    sizes: dict[tuple[int, int], int],
    hospital_id: int | None,
    match_scores: dict[str, float],
    specialization: str | None = None,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for doctor, hospital, user in rows:
        # hospital_specific joins pin the patient to one centre.
        if hospital_id is not None and hospital.id != hospital_id:
            continue
        # Severity lock: only doctors of the required specialisation.
        if specialization is not None and doctor.specialization != specialization:
            continue
        candidates.append(
            Candidate(
                doctor_id=doctor.id,
                doctor_name=user.full_name or "Doctor",
                specialization=doctor.specialization,
                hospital_id=hospital.id,
                hospital_name=hospital.name,
                queue_size=sizes.get((doctor.id, hospital.id), 0),
                avg_consultation_minutes=doctor.avg_consultation_minutes or 12.0,
                hospital_lat=hospital.latitude,
                hospital_lng=hospital.longitude,
                match_score=match_scores.get(doctor.specialization, 0.6),
            )
        )
    return candidates


async def build_candidates(
    db: AsyncSession,
    doctor_ids: list[int],
    hospital_id: int | None,
    match_scores: dict[str, float],
    exclude_entry_id: int | None = None,
) -> list[Candidate]:
    rows = await doctor_locations(db, doctor_ids)
    sizes = await queue_sizes(db, exclude_entry_id=exclude_entry_id)
    return _rows_to_candidates(rows, sizes, hospital_id, match_scores)


async def build_specialist_candidates(
    db: AsyncSession,
    specialization: str,
    hospital_id: int | None,
    match_scores: dict[str, float],
    exclude_entry_id: int | None = None,
) -> list[Candidate]:
    """Every available doctor of one specialisation, network-wide.

    Used when a severity-locked patient's own picks contain no doctor of the
    specialisation triage demands -- rather than bounce them back to the wizard,
    we widen the search to the whole network.
    """
    rows = await doctor_locations(db, None)
    sizes = await queue_sizes(db, exclude_entry_id=exclude_entry_id)
    return _rows_to_candidates(rows, sizes, hospital_id, match_scores, specialization=specialization)


async def select_candidates(
    db: AsyncSession,
    doctor_ids: list[int],
    hospital_id: int | None,
    match_scores: dict[str, float],
    ai_recommendation: dict | None,
    exclude_entry_id: int | None = None,
) -> tuple[list[Candidate], str | None]:
    """Candidate set for assignment, applying the severity lock.

    Returns (candidates, required_specialization). When urgency is emergency or
    high, candidates are restricted to the top recommended specialisation --
    from the patient's picks if any match, otherwise from the whole network. An
    empty list means no such specialist is available anywhere; the caller turns
    that into a soft error rather than silently assigning a generalist.
    """
    candidates = await build_candidates(db, doctor_ids, hospital_id, match_scores, exclude_entry_id)

    if not is_severity_locked(ai_recommendation):
        return candidates, None

    spec = top_specialization(ai_recommendation)
    if spec is None:
        return candidates, None

    matching = [c for c in candidates if c.specialization == spec]
    if not matching:
        matching = await build_specialist_candidates(
            db, spec, hospital_id, match_scores, exclude_entry_id
        )
    return matching, spec


async def next_queue_number(db: AsyncSession, doctor_id: int, hospital_id: int) -> int:
    """Token number, restarting each day per (doctor, hospital).

    Not globally unique -- it is the number called out in a waiting room, so it
    must stay small and human-readable.
    """
    today = date.today()
    result = await db.execute(
        select(func.max(QueueEntry.queue_number)).where(
            QueueEntry.doctor_id == doctor_id,
            QueueEntry.hospital_id == hospital_id,
            func.date(QueueEntry.joined_at) == today,
        )
    )
    return int(result.scalar() or 0) + 1


async def position_in_queue(db: AsyncSession, entry: QueueEntry) -> int:
    """1-based position. Emergencies and higher priorities jump the line;
    within a priority it is first-come, first-served."""
    if entry.status == QueueStatus.IN_CONSULTATION:
        return 0

    rows = await db.execute(
        select(QueueEntry.priority, QueueEntry.joined_at, QueueEntry.id).where(
            QueueEntry.doctor_id == entry.doctor_id,
            QueueEntry.hospital_id == entry.hospital_id,
            QueueEntry.status == QueueStatus.WAITING,
        )
    )
    ahead = 0
    for priority, joined_at, entry_id in rows.all():
        if entry_id == entry.id:
            continue
        if priority.weight > entry.priority.weight:
            ahead += 1
        elif priority.weight == entry.priority.weight and joined_at < entry.joined_at:
            ahead += 1
    return ahead + 1


async def estimate_wait(db: AsyncSession, entry: QueueEntry) -> tuple[int, float | None]:
    """Return (wait_minutes, travel_minutes) for an entry."""
    doctor = await db.get(Doctor, entry.doctor_id)
    hospital = await db.get(Hospital, entry.hospital_id)

    position = await position_in_queue(db, entry)
    ahead = max(0, position - 1)
    avg = (doctor.avg_consultation_minutes if doctor else 12.0) or 12.0
    wait = ahead * avg

    travel = travel_minutes(
        entry.patient_latitude,
        entry.patient_longitude,
        hospital.latitude if hospital else None,
        hospital.longitude if hospital else None,
    )
    return int(round(wait)), travel


async def join_queue(
    db: AsyncSession,
    patient: Patient,
    doctor_ids: list[int],
    hospital_id: int | None,
    queue_type: QueueType,
    priority: QueuePriority,
    symptoms_description: str,
    ai_recommendation: dict | None,
    patient_lat: float | None,
    patient_lng: float | None,
) -> tuple[QueueEntry | None, list[ScoredCandidate], str | None]:
    """Assign the patient to the best (doctor x hospital) pair.

    Returns (entry, ranked_candidates, error).
    """
    # One active entry per patient. Without this a patient can silently hold
    # slots across several doctors and inflate every queue they touch.
    existing = await db.execute(
        select(QueueEntry).where(
            QueueEntry.patient_id == patient.id,
            QueueEntry.status.in_(ACTIVE_STATUSES),
        )
    )
    if existing.scalars().first() is not None:
        return None, [], "You are already in a queue. Cancel it before joining another."

    match_scores = match_scores_from_ai(ai_recommendation)
    candidates, required_spec = await select_candidates(
        db, doctor_ids, hospital_id, match_scores, ai_recommendation
    )
    if not candidates:
        if required_spec is not None:
            return None, [], (
                f"No {required_spec} is available right now. If this is an emergency, "
                f"go to the nearest emergency department immediately."
            )
        return None, [], "None of the selected doctors are currently available."

    ranked = rank_candidates(candidates, patient_lat, patient_lng)
    best = ranked[0]

    # Triage red flags outrank whatever the client sent: a client that posts
    # priority "normal" for a stroke must not be taken at its word.
    urgency = (ai_recommendation or {}).get("urgency_level")
    if urgency == "emergency":
        priority = QueuePriority.EMERGENCY
    elif urgency == "high" and priority.weight < QueuePriority.HIGH.weight:
        priority = QueuePriority.HIGH

    entry = QueueEntry(
        patient_id=patient.id,
        doctor_id=best.candidate.doctor_id,
        hospital_id=best.candidate.hospital_id,
        queue_type=queue_type,
        queue_number=await next_queue_number(db, best.candidate.doctor_id, best.candidate.hospital_id),
        status=QueueStatus.WAITING,
        priority=priority,
        is_emergency=priority is QueuePriority.EMERGENCY,
        symptoms_description=symptoms_description,
        ai_summary=(ai_recommendation or {}).get("summary"),
        ai_recommendation=ai_recommendation,
        candidate_doctor_ids=doctor_ids,
        patient_latitude=patient_lat,
        patient_longitude=patient_lng,
        leave_at=compute_leave_at(best.wait_minutes, best.travel_minutes),
    )
    if should_lock_in(best.wait_minutes, best.travel_minutes):
        entry.locked_in_at = datetime.now(UTC)

    db.add(entry)
    await db.commit()
    await db.refresh(entry)

    await publish_doctor_event(
        entry.doctor_id,
        EVENT_POSITION_CHANGED,
        {"queue_id": entry.id, "hospital_id": entry.hospital_id},
    )
    return entry, ranked, None


async def rescore_entry(db: AsyncSession, entry: QueueEntry) -> bool:
    """Re-evaluate one waiting entry; move it if a clearly better centre exists.

    Returns True if the entry was moved.
    """
    if entry.status != QueueStatus.WAITING:
        return False
    # Already travelling: being re-routed mid-journey is worse than waiting.
    if entry.locked_in_at is not None:
        return False
    # Emergencies are never shuffled -- continuity beats optimisation.
    if entry.priority is QueuePriority.EMERGENCY:
        return False
    # Only cross-hospital entries float; a hospital-specific join is a promise.
    if entry.queue_type is not QueueType.CROSS_HOSPITAL:
        return False

    candidate_ids = entry.candidate_doctor_ids or [entry.doctor_id]
    match_scores = match_scores_from_ai(entry.ai_recommendation)
    candidates, _ = await select_candidates(
        db, candidate_ids, None, match_scores, entry.ai_recommendation, exclude_entry_id=entry.id
    )
    if not candidates:
        return False

    ranked = rank_candidates(candidates, entry.patient_latitude, entry.patient_longitude)
    best = ranked[0]

    current = next(
        (
            c
            for c in ranked
            if c.candidate.doctor_id == entry.doctor_id and c.candidate.hospital_id == entry.hospital_id
        ),
        None,
    )
    if current is None:
        # Current doctor is no longer available anywhere -- move regardless.
        current_score = float("inf")
    else:
        current_score = current.score

    # Lock in before considering a move: once the patient should be travelling,
    # freeze where they are rather than sending them somewhere new.
    if current is not None and should_lock_in(current.wait_minutes, current.travel_minutes):
        entry.locked_in_at = datetime.now(UTC)
        entry.leave_at = compute_leave_at(current.wait_minutes, current.travel_minutes)
        await db.commit()
        return False

    moved = False
    if (
        best.candidate.doctor_id != entry.doctor_id or best.candidate.hospital_id != entry.hospital_id
    ) and is_worth_moving(current_score, best.score):
        previous_hospital = entry.hospital_id
        previous_doctor = entry.doctor_id

        entry.doctor_id = best.candidate.doctor_id
        entry.hospital_id = best.candidate.hospital_id
        entry.queue_number = await next_queue_number(db, best.candidate.doctor_id, best.candidate.hospital_id)
        entry.reassignment_count += 1
        moved = True

        await publish_queue_event(
            entry.id,
            EVENT_REASSIGNED,
            {
                "queue_id": entry.id,
                "doctor_id": entry.doctor_id,
                "doctor_name": best.candidate.doctor_name,
                "hospital_id": entry.hospital_id,
                "hospital_name": best.candidate.hospital_name,
                "estimated_wait_time": int(round(best.wait_minutes)),
                "reason": "A nearer or faster centre became available.",
            },
        )
        # Both queues changed shape.
        await publish_doctor_event(
            previous_doctor, EVENT_POSITION_CHANGED, {"hospital_id": previous_hospital}
        )
        await publish_doctor_event(
            entry.doctor_id, EVENT_POSITION_CHANGED, {"hospital_id": entry.hospital_id}
        )

    entry.leave_at = compute_leave_at(best.wait_minutes, best.travel_minutes)
    if should_lock_in(best.wait_minutes, best.travel_minutes):
        entry.locked_in_at = datetime.now(UTC)

    await db.commit()
    return moved


async def rescore_all_waiting(db: AsyncSession) -> int:
    """Sweep every waiting cross-hospital entry. Returns how many moved."""
    result = await db.execute(
        select(QueueEntry).where(
            QueueEntry.status == QueueStatus.WAITING,
            QueueEntry.queue_type == QueueType.CROSS_HOSPITAL,
            QueueEntry.locked_in_at.is_(None),
        )
    )
    moved = 0
    for entry in result.scalars().all():
        try:
            if await rescore_entry(db, entry):
                moved += 1
        except Exception:
            await db.rollback()
            logger.warning("Re-scoring failed for queue entry %s", entry.id, exc_info=True)
    return moved


def candidate_to_info(scored: ScoredCandidate) -> dict:
    return {
        "doctor_id": scored.candidate.doctor_id,
        "doctor_name": scored.candidate.doctor_name,
        "specialization": scored.candidate.specialization,
        "match_score": round(scored.candidate.match_score, 2),
        "queue_size": scored.candidate.queue_size,
        "estimated_wait_time": int(round(scored.wait_minutes)),
        "hospital_id": scored.candidate.hospital_id,
        "hospital_name": scored.candidate.hospital_name,
        "distance": scored.distance_km,
    }


__all__ = [
    "ACTIVE_STATUSES",
    "build_candidates",
    "build_specialist_candidates",
    "candidate_to_info",
    "doctor_locations",
    "estimate_wait",
    "join_queue",
    "match_scores_from_ai",
    "next_queue_number",
    "position_in_queue",
    "queue_size_for",
    "queue_sizes",
    "rescore_all_waiting",
    "rescore_entry",
    "score_candidate",
    "select_candidates",
]
