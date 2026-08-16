from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from src.core.deps import CurrentDoctor, CurrentPatient, CurrentUser, DbSession
from src.models.enums import QueueStatus, QueueType, UserRole
from src.models.organizations import Hospital
from src.models.people import Doctor, Patient
from src.models.queue import QueueEntry
from src.models.user import User
from src.schemas.healthcare import HospitalResponse
from src.schemas.queue import (
    AvailableDoctorResponse,
    AvailableDoctorsResponse,
    DoctorQueueResponse,
    JoinQueueRequest,
    JoinQueueResponse,
    MyQueuesResponse,
    PatientQueueInfo,
    QueueStatusResponse,
    UpdateQueueStatusRequest,
)
from src.services.events import (
    EVENT_CALLED,
    EVENT_CANCELLED,
    EVENT_COMPLETED,
    EVENT_POSITION_CHANGED,
    publish_doctor_event,
    publish_queue_event,
)
from src.services.queue.engine import (
    ACTIVE_STATUSES,
    build_candidates,
    candidate_to_info,
    estimate_wait,
    join_queue,
    position_in_queue,
    queue_sizes,
)
from src.services.queue.scoring import haversine_km, rank_candidates

router = APIRouter(prefix="/api/queue", tags=["Queue"])


async def _to_status(db: DbSession, entry: QueueEntry) -> QueueStatusResponse:
    doctor = await db.get(Doctor, entry.doctor_id)
    hospital = await db.get(Hospital, entry.hospital_id)
    doctor_user = await db.get(User, doctor.user_id) if doctor else None

    wait, travel = await estimate_wait(db, entry)
    position = await position_in_queue(db, entry)

    distance = None
    if None not in (entry.patient_latitude, entry.patient_longitude) and hospital and hospital.latitude:
        distance = round(
            haversine_km(
                entry.patient_latitude, entry.patient_longitude, hospital.latitude, hospital.longitude
            ),
            2,
        )

    return QueueStatusResponse(
        queue_id=entry.id,
        patient_id=entry.patient_id,
        doctor_id=entry.doctor_id,
        doctor_name=(doctor_user.full_name if doctor_user else None) or "Doctor",
        doctor_specialization=doctor.specialization if doctor else "",
        queue_type=entry.queue_type,
        hospital_id=entry.hospital_id,
        hospital_name=hospital.name if hospital else None,
        # The frontend reads either spelling depending on the screen; serve both.
        assigned_hospital_id=entry.hospital_id,
        assigned_hospital_name=hospital.name if hospital else None,
        queue_number=entry.queue_number,
        current_position=position,
        status=entry.status,
        priority=entry.priority,
        estimated_wait_time=wait,
        joined_at=entry.joined_at,
        consultation_started_at=entry.consultation_started_at,
        consultation_completed_at=entry.consultation_completed_at,
        patient_notified_at=entry.patient_notified_at,
        is_emergency=entry.is_emergency,
        ai_summary=entry.ai_summary,
        symptoms_description=entry.symptoms_description,
        leave_at=entry.leave_at,
        travel_time_minutes=int(round(travel)) if travel is not None else None,
        distance_km=distance,
        locked_in=entry.locked_in_at is not None,
    )


@router.post("/join", response_model=JoinQueueResponse)
async def join(payload: JoinQueueRequest, patient: CurrentPatient, db: DbSession):
    """Join a queue. The server picks the best doctor and hospital from the
    candidates offered and returns the runners-up as alternatives."""
    location = payload.patient_location
    entry, ranked, error = await join_queue(
        db,
        patient=patient,
        doctor_ids=payload.doctor_ids,
        hospital_id=payload.hospital_id,
        queue_type=payload.queue_type,
        priority=payload.priority,
        symptoms_description=payload.symptoms_description,
        ai_recommendation=payload.ai_recommendation,
        patient_lat=location.latitude if location else None,
        patient_lng=location.longitude if location else None,
    )

    if entry is None:
        # A soft failure the wizard can render, not an HTTP error -- the client
        # reads `success` and shows `error` inline.
        return JoinQueueResponse(success=False, error=error, suggested_doctors=[])

    best = ranked[0]
    detail = await _to_status(db, entry)

    return JoinQueueResponse(
        success=True,
        queue_id=entry.id,
        doctor_id=entry.doctor_id,
        doctor_name=detail.doctor_name,
        doctor_specialization=detail.doctor_specialization,
        queue_type=entry.queue_type,
        queue_number=entry.queue_number,
        estimated_wait_time=detail.estimated_wait_time,
        current_position=detail.current_position,
        assigned_hospital_id=entry.hospital_id,
        assigned_hospital_name=detail.hospital_name,
        alternative_options=[candidate_to_info(c) for c in ranked[1:4]],
        leave_at=entry.leave_at,
        travel_time_minutes=int(round(best.travel_minutes)) if best.travel_minutes is not None else None,
    )


@router.get("/my-queues", response_model=MyQueuesResponse)
async def my_queues(patient: CurrentPatient, db: DbSession):
    """Envelope, not a bare array -- my-queues/page.tsx reads `response.queues`."""
    result = await db.execute(
        select(QueueEntry)
        .where(QueueEntry.patient_id == patient.id)
        .order_by(QueueEntry.joined_at.desc())
        .limit(50)
    )
    return MyQueuesResponse(queues=[await _to_status(db, e) for e in result.scalars().all()])


@router.get("/hospitals", response_model=list[HospitalResponse])
async def queue_hospitals(user: CurrentUser, db: DbSession):
    result = await db.execute(select(Hospital).where(Hospital.is_active.is_(True)).order_by(Hospital.name))
    return [HospitalResponse.model_validate(h) for h in result.scalars().all()]


@router.get("/available-doctors", response_model=AvailableDoctorsResponse)
async def available_doctors(
    user: CurrentUser,
    db: DbSession,
    queue_type: QueueType = QueueType.HOSPITAL_SPECIFIC,
    hospital_id: int | None = None,
    specialization: str | None = None,
    # Comma-joined by the client, not a repeated query param.
    specializations: str | None = None,
    include_general: bool = False,
    # Additive: the browser already holds the patient's coordinates in
    # sessionStorage, so it can rank this list by real travel time too.
    latitude: float | None = Query(default=None, ge=-90, le=90),
    longitude: float | None = Query(default=None, ge=-180, le=180),
):
    """Doctors the patient can pick from, ranked by predicted total wait."""
    wanted: list[str] = []
    if specializations:
        wanted = [s.strip() for s in specializations.split(",") if s.strip()]
    elif specialization:
        wanted = [specialization]
    if include_general and "General Medicine" not in wanted:
        wanted.append("General Medicine")

    stmt = (
        select(Doctor, Hospital, User)
        .join(Hospital, Doctor.hospital_id == Hospital.id)
        .join(User, Doctor.user_id == User.id)
        .where(Doctor.is_active.is_(True), Hospital.is_active.is_(True), User.is_active.is_(True))
    )
    if hospital_id is not None:
        stmt = stmt.where(Hospital.id == hospital_id)
    if wanted:
        stmt = stmt.where(Doctor.specialization.in_(wanted))

    rows = (await db.execute(stmt)).all()
    sizes = await queue_sizes(db)

    # A cross-hospital search is not tied to one centre; the response shape
    # still requires a hospital, so report the one we searched or a sentinel.
    header_hospital_id = hospital_id or 0
    header_hospital_name = "All hospitals"
    if hospital_id is not None:
        hospital = await db.get(Hospital, hospital_id)
        if hospital is None:
            raise HTTPException(status_code=404, detail="Hospital not found")
        header_hospital_name = hospital.name

    # Rank with the same engine the assignment uses, so the list the patient
    # sees is ordered the way the server would choose.
    match_scores = {s: 1.0 for s in wanted}
    doctor_ids = [doctor.id for doctor, _, _ in rows]
    candidates = await build_candidates(db, doctor_ids, hospital_id, match_scores) if doctor_ids else []
    ranked = rank_candidates(candidates, latitude, longitude)
    order = {(c.candidate.doctor_id, c.candidate.hospital_id): i for i, c in enumerate(ranked)}
    scored_by_key = {(c.candidate.doctor_id, c.candidate.hospital_id): c for c in ranked}

    doctors: list[AvailableDoctorResponse] = []
    for doctor, hospital, doctor_user in rows:
        key = (doctor.id, hospital.id)
        scored = scored_by_key.get(key)
        queue_size = sizes.get(key, 0)
        doctors.append(
            AvailableDoctorResponse(
                doctor_id=doctor.id,
                name=doctor_user.full_name or "Doctor",
                specialization=doctor.specialization,
                qualification=doctor.qualification,
                years_of_experience=doctor.years_of_experience,
                consultation_fee=doctor.consultation_fee,
                current_queue_size=queue_size,
                estimated_wait_time=int(round(scored.wait_minutes))
                if scored
                else int(queue_size * (doctor.avg_consultation_minutes or 12.0)),
                availability_status="available",
                match_score=1.0 if doctor.specialization in wanted else 0.6,
                distance=scored.distance_km if scored else None,
                hospital_id=hospital.id,
                hospital_name=hospital.name,
            )
        )

    doctors.sort(key=lambda d: order.get((d.doctor_id, d.hospital_id), 10_000))

    return AvailableDoctorsResponse(
        hospital_id=header_hospital_id,
        hospital_name=header_hospital_name,
        doctors=doctors,
        total_available=len(doctors),
    )


@router.get("/status/{queue_id}", response_model=QueueStatusResponse)
async def queue_status(queue_id: int, user: CurrentUser, db: DbSession):
    entry = await db.get(QueueEntry, queue_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Queue entry not found")

    if user.role == UserRole.PATIENT:
        row = await db.execute(select(Patient).where(Patient.user_id == user.id))
        patient = row.scalar_one_or_none()
        if patient is None or entry.patient_id != patient.id:
            raise HTTPException(status_code=403, detail="This is not your queue entry")
    elif user.role == UserRole.DOCTOR:
        row = await db.execute(select(Doctor).where(Doctor.user_id == user.id))
        doctor = row.scalar_one_or_none()
        if doctor is None or entry.doctor_id != doctor.id:
            raise HTTPException(status_code=403, detail="This patient is not in your queue")
    elif user.role not in (UserRole.ADMIN, UserRole.NURSE):
        raise HTTPException(status_code=403, detail="You do not have access to this queue entry")

    return await _to_status(db, entry)


@router.delete("/cancel/{queue_id}")
async def cancel_queue(queue_id: int, patient: CurrentPatient, db: DbSession):
    entry = await db.get(QueueEntry, queue_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Queue entry not found")
    if entry.patient_id != patient.id:
        raise HTTPException(status_code=403, detail="This is not your queue entry")
    if entry.status is QueueStatus.IN_CONSULTATION:
        raise HTTPException(
            status_code=409, detail="Your consultation has already started and cannot be cancelled here"
        )
    if entry.status is not QueueStatus.WAITING:
        raise HTTPException(status_code=409, detail=f"This entry is already {entry.status.value}")

    entry.status = QueueStatus.CANCELLED
    await db.commit()

    await publish_queue_event(entry.id, EVENT_CANCELLED, {"queue_id": entry.id})
    await publish_doctor_event(entry.doctor_id, EVENT_POSITION_CHANGED, {"hospital_id": entry.hospital_id})
    return {"success": True, "queue_id": entry.id, "status": entry.status.value}


@router.get("/doctor/{doctor_id}", response_model=DoctorQueueResponse)
async def doctor_queue(
    doctor_id: int,
    user: CurrentUser,
    db: DbSession,
    hospital_id: int | None = None,
    queue_type: QueueType | None = None,
):
    """The doctor's working list. Ordered the way patients are actually called:
    priority first, then arrival."""
    if user.role == UserRole.DOCTOR:
        row = await db.execute(select(Doctor).where(Doctor.user_id == user.id))
        me = row.scalar_one_or_none()
        if me is None or me.id != doctor_id:
            raise HTTPException(status_code=403, detail="You can only view your own queue")
    elif user.role not in (UserRole.ADMIN, UserRole.NURSE):
        raise HTTPException(status_code=403, detail="You do not have access to this queue")

    stmt = select(QueueEntry).where(
        QueueEntry.doctor_id == doctor_id,
        QueueEntry.status.in_(ACTIVE_STATUSES),
    )
    if hospital_id is not None:
        stmt = stmt.where(QueueEntry.hospital_id == hospital_id)
    if queue_type is not None:
        stmt = stmt.where(QueueEntry.queue_type == queue_type)

    entries = list((await db.execute(stmt)).scalars().all())
    entries.sort(key=lambda e: (-e.priority.weight, e.joined_at))

    queue: list[PatientQueueInfo] = []
    for entry in entries:
        patient = await db.get(Patient, entry.patient_id)
        hospital = await db.get(Hospital, entry.hospital_id)
        wait, _ = await estimate_wait(db, entry)
        queue.append(
            PatientQueueInfo(
                queue_id=entry.id,
                patient_uhid=patient.uhid if patient else "",
                patient_name=patient.full_name if patient else "Unknown",
                queue_number=entry.queue_number,
                queue_type=entry.queue_type,
                status=entry.status,
                priority=entry.priority,
                symptoms_description=entry.symptoms_description,
                estimated_wait_time=wait,
                joined_at=entry.joined_at,
                is_emergency=entry.is_emergency,
                hospital_name=hospital.name if hospital else None,
                assigned_hospital_name=hospital.name if hospital else None,
                ai_summary=entry.ai_summary,
            )
        )

    return DoctorQueueResponse(
        doctor_id=doctor_id,
        hospital_id=hospital_id,
        queue_type=queue_type,
        queue=queue,
        total_waiting=sum(1 for e in entries if e.status is QueueStatus.WAITING),
        total_in_consultation=sum(1 for e in entries if e.status is QueueStatus.IN_CONSULTATION),
    )


# Which transitions a doctor may make. Anything not listed is rejected rather
# than silently applied -- a queue that can jump from `completed` back to
# `waiting` corrupts every position calculation behind it.
ALLOWED_TRANSITIONS: dict[QueueStatus, set[QueueStatus]] = {
    QueueStatus.WAITING: {QueueStatus.IN_CONSULTATION, QueueStatus.NO_SHOW, QueueStatus.CANCELLED},
    QueueStatus.IN_CONSULTATION: {QueueStatus.COMPLETED, QueueStatus.CANCELLED},
    QueueStatus.COMPLETED: set(),
    QueueStatus.CANCELLED: set(),
    QueueStatus.NO_SHOW: {QueueStatus.WAITING},
}


@router.patch("/status/{queue_id}", status_code=status.HTTP_200_OK)
async def update_queue_status(
    queue_id: int,
    payload: UpdateQueueStatusRequest,
    doctor: CurrentDoctor,
    db: DbSession,
):
    entry = await db.get(QueueEntry, queue_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Queue entry not found")
    if entry.doctor_id != doctor.id:
        raise HTTPException(status_code=403, detail="This patient is not in your queue")

    new_status = payload.new_status
    if new_status not in ALLOWED_TRANSITIONS[entry.status]:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot move a {entry.status.value} entry to {new_status.value}",
        )

    now = datetime.now(UTC)
    entry.status = new_status
    if payload.notes:
        entry.notes = payload.notes

    if new_status is QueueStatus.IN_CONSULTATION:
        entry.consultation_started_at = now
        entry.patient_notified_at = now
        entry.locked_in_at = entry.locked_in_at or now
        await publish_queue_event(
            entry.id, EVENT_CALLED, {"queue_id": entry.id, "doctor_id": entry.doctor_id}
        )
    elif new_status is QueueStatus.COMPLETED:
        entry.consultation_completed_at = now
        await _update_average_consultation_time(db, doctor, entry)
        await publish_queue_event(entry.id, EVENT_COMPLETED, {"queue_id": entry.id})
    elif new_status is QueueStatus.CANCELLED:
        await publish_queue_event(entry.id, EVENT_CANCELLED, {"queue_id": entry.id})

    await db.commit()

    # Everyone behind this patient just moved up.
    await publish_doctor_event(entry.doctor_id, EVENT_POSITION_CHANGED, {"hospital_id": entry.hospital_id})
    return {"success": True, "queue_id": entry.id, "status": entry.status.value}


async def _update_average_consultation_time(db: DbSession, doctor: Doctor, entry: QueueEntry) -> None:
    """Feed the actual duration back into the estimate.

    Exponential moving average: recent consultations matter more, and one
    outlier cannot swing the whole queue's predictions.
    """
    if entry.consultation_started_at is None or entry.consultation_completed_at is None:
        return
    started = entry.consultation_started_at
    completed = entry.consultation_completed_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=UTC)

    minutes = (completed - started).total_seconds() / 60.0
    # Ignore implausible durations: a mis-click or a forgotten "complete"
    # shouldn't teach the model that consultations take eight hours.
    if not (1.0 <= minutes <= 120.0):
        return

    current = doctor.avg_consultation_minutes or 12.0
    doctor.avg_consultation_minutes = round(0.8 * current + 0.2 * minutes, 2)
