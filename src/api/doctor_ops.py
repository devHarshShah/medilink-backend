"""Doctor presence, schedules and stats.

Mounted under /api/doctor alongside the clinical routes.
"""

from datetime import UTC, date, datetime, time

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, or_, select

from src.core.deps import CurrentDoctor, CurrentPharmacy, DbSession
from src.models.clinical import LabReport, MedicalRecord, Prescription
from src.models.doctor_ops import DoctorCheckIn, DoctorLeave, DoctorSchedule
from src.models.enums import (
    DoctorAvailabilityStatus,
    PrescriptionStatus,
    QueueStatus,
    TestStatus,
)
from src.models.organizations import Hospital
from src.models.queue import QueueEntry
from src.models.user import User
from src.schemas.doctor_ops import (
    DAY_NAMES,
    AvailabilityEntry,
    CheckInRequest,
    CheckOutRequest,
    CurrentHospital,
    DoctorHospitalResponse,
    DoctorStatsResponse,
    DoctorStatusResponse,
    LeaveCreate,
    LeaveResponse,
    PharmacyStatsResponse,
    PresenceStatusRequest,
    ScheduleCreate,
    ScheduleResponse,
    ScheduleToday,
    UpcomingScheduleEntry,
)
from src.services.events import EVENT_QUEUE_UPDATED, publish_doctor_event
from src.services.queue.engine import queue_sizes

router = APIRouter(prefix="/api/doctor", tags=["Doctor"])


def _hhmm(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value else None


async def _open_checkins(db: DbSession, doctor_id: int) -> list[DoctorCheckIn]:
    result = await db.execute(
        select(DoctorCheckIn).where(
            DoctorCheckIn.doctor_id == doctor_id,
            DoctorCheckIn.check_out_time.is_(None),
        )
    )
    return list(result.scalars().all())


async def _schedule_today(db: DbSession, doctor_id: int, hospital_id: int) -> ScheduleToday | None:
    today = date.today()
    result = await db.execute(
        select(DoctorSchedule).where(
            DoctorSchedule.doctor_id == doctor_id,
            DoctorSchedule.hospital_id == hospital_id,
            DoctorSchedule.day_of_week == today.weekday(),
            DoctorSchedule.is_available.is_(True),
            or_(DoctorSchedule.effective_from.is_(None), DoctorSchedule.effective_from <= today),
            or_(DoctorSchedule.effective_until.is_(None), DoctorSchedule.effective_until >= today),
        )
    )
    schedule = result.scalars().first()
    if schedule is None:
        return None
    return ScheduleToday(
        start_time=_hhmm(schedule.start_time) or "",
        end_time=_hhmm(schedule.end_time) or "",
        break_start_time=_hhmm(schedule.break_start_time),
        break_end_time=_hhmm(schedule.break_end_time),
        slot_duration=schedule.slot_duration,
    )


# --------------------------------------------------------------------------
# Presence
# --------------------------------------------------------------------------
@router.get("/hospitals", response_model=list[DoctorHospitalResponse])
async def doctor_hospitals(doctor: CurrentDoctor, db: DbSession):
    """Every hospital this doctor can work at, with live presence and queue depth."""
    open_checkins = {c.hospital_id: c for c in await _open_checkins(db, doctor.id)}
    sizes = await queue_sizes(db)

    result = await db.execute(select(Hospital).where(Hospital.is_active.is_(True)).order_by(Hospital.name))
    hospitals = list(result.scalars().all())

    current = next((h for h in hospitals if h.id in open_checkins), None)
    avg = doctor.avg_consultation_minutes or 12.0

    payload: list[DoctorHospitalResponse] = []
    for hospital in hospitals:
        checkin = open_checkins.get(hospital.id)
        queue_size = sizes.get((doctor.id, hospital.id), 0)

        distance = None
        if current is not None and current.id != hospital.id:
            if None not in (current.latitude, current.longitude, hospital.latitude, hospital.longitude):
                from src.services.queue.scoring import haversine_km

                distance = round(
                    haversine_km(current.latitude, current.longitude, hospital.latitude, hospital.longitude),
                    2,
                )

        payload.append(
            DoctorHospitalResponse(
                hospital_id=hospital.id,
                hospital_name=hospital.name,
                hospital_address=hospital.address,
                is_checked_in=checkin is not None,
                presence_status=(
                    checkin.presence_status.value if checkin else DoctorAvailabilityStatus.OFFLINE.value
                ),
                current_queue_size=queue_size,
                estimated_wait_time=int(queue_size * avg),
                active_checkin_id=checkin.id if checkin else None,
                schedule_today=await _schedule_today(db, doctor.id, hospital.id),
                distance_from_current=distance,
            )
        )
    return payload


@router.get("/status", response_model=DoctorStatusResponse)
async def doctor_status(doctor: CurrentDoctor, db: DbSession):
    user = await db.get(User, doctor.user_id)
    open_checkins = await _open_checkins(db, doctor.id)
    sizes = await queue_sizes(db)
    avg = doctor.avg_consultation_minutes or 12.0

    current_hospital = None
    presence = DoctorAvailabilityStatus.OFFLINE.value
    last_activity = None

    availabilities: list[AvailabilityEntry] = []
    for checkin in open_checkins:
        hospital = await db.get(Hospital, checkin.hospital_id)
        if hospital is None:
            continue
        queue_size = sizes.get((doctor.id, hospital.id), 0)
        availabilities.append(
            AvailabilityEntry(
                hospital_id=hospital.id,
                hospital_name=hospital.name,
                is_checked_in=True,
                presence_status=checkin.presence_status.value,
                current_queue_size=queue_size,
                estimated_wait_time=int(queue_size * avg),
            )
        )
        if current_hospital is None:
            check_in_time = checkin.check_in_time
            if check_in_time.tzinfo is None:
                check_in_time = check_in_time.replace(tzinfo=UTC)
            current_hospital = CurrentHospital(
                hospital_id=hospital.id,
                hospital_name=hospital.name,
                check_in_time=checkin.check_in_time,
                duration_minutes=int((datetime.now(UTC) - check_in_time).total_seconds() / 60),
            )
            presence = checkin.presence_status.value
            last_activity = checkin.last_activity

    return DoctorStatusResponse(
        doctor_id=doctor.id,
        doctor_name=(user.full_name if user else None) or "Doctor",
        is_checked_in=bool(open_checkins),
        current_hospital=current_hospital,
        presence_status=presence,
        last_activity=last_activity,
        availabilities=availabilities,
    )


@router.post("/check-in", status_code=status.HTTP_201_CREATED)
async def check_in(payload: CheckInRequest, doctor: CurrentDoctor, db: DbSession):
    hospital = await db.get(Hospital, payload.hospital_id)
    if hospital is None or not hospital.is_active:
        raise HTTPException(status_code=404, detail="Hospital not found")

    existing = await _open_checkins(db, doctor.id)
    if any(c.hospital_id == payload.hospital_id for c in existing):
        raise HTTPException(status_code=409, detail=f"You are already checked in at {hospital.name}")

    # A doctor is only ever in one building. An open check-in elsewhere means
    # they forgot to check out, so close it rather than showing them as
    # simultaneously available in two places.
    now = datetime.now(UTC)
    for stale in existing:
        stale.check_out_time = now
        stale.notes = (stale.notes or "") + " [auto-closed on check-in elsewhere]"

    checkin = DoctorCheckIn(
        doctor_id=doctor.id,
        hospital_id=payload.hospital_id,
        presence_status=DoctorAvailabilityStatus.AVAILABLE,
        last_activity=now,
    )
    db.add(checkin)
    await db.commit()
    await db.refresh(checkin)

    await publish_doctor_event(
        doctor.id, EVENT_QUEUE_UPDATED, {"hospital_id": payload.hospital_id, "checked_in": True}
    )
    return {
        "success": True,
        "checkin_id": checkin.id,
        "hospital_id": hospital.id,
        "hospital_name": hospital.name,
        "auto_closed": [c.id for c in existing],
    }


@router.post("/check-out")
async def check_out(payload: CheckOutRequest, doctor: CurrentDoctor, db: DbSession):
    open_checkins = await _open_checkins(db, doctor.id)
    if not open_checkins:
        raise HTTPException(status_code=409, detail="You are not currently checked in anywhere")

    now = datetime.now(UTC)
    closed = []
    for checkin in open_checkins:
        checkin.check_out_time = now
        checkin.notes = payload.notes or checkin.notes
        checkin.next_hospital_id = payload.next_hospital_id
        closed.append(checkin.hospital_id)

    await db.commit()

    # Patients waiting for this doctor here may now be better served elsewhere.
    from src.services.queue.engine import rescore_all_waiting

    try:
        moved = await rescore_all_waiting(db)
    except Exception:
        moved = 0

    for hospital_id in closed:
        await publish_doctor_event(
            doctor.id, EVENT_QUEUE_UPDATED, {"hospital_id": hospital_id, "checked_in": False}
        )

    return {
        "success": True,
        "checked_out_from": closed,
        "next_hospital_id": payload.next_hospital_id,
        "patients_reassigned": moved,
    }


@router.put("/presence-status")
async def update_presence(payload: PresenceStatusRequest, doctor: CurrentDoctor, db: DbSession):
    open_checkins = await _open_checkins(db, doctor.id)
    if not open_checkins:
        raise HTTPException(status_code=409, detail="Check in before setting your presence status")

    new_status = payload.normalized()
    now = datetime.now(UTC)
    for checkin in open_checkins:
        checkin.presence_status = new_status
        checkin.last_activity = now
    await db.commit()

    await publish_doctor_event(doctor.id, EVENT_QUEUE_UPDATED, {"presence_status": new_status.value})
    return {"success": True, "presence_status": new_status.value}


# --------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------
def _schedule_to_response(schedule: DoctorSchedule, hospital_name: str) -> ScheduleResponse:
    return ScheduleResponse(
        schedule_id=schedule.id,
        hospital_id=schedule.hospital_id,
        hospital_name=hospital_name,
        day_of_week=schedule.day_of_week,
        day_name=DAY_NAMES[schedule.day_of_week],
        start_time=_hhmm(schedule.start_time) or "",
        end_time=_hhmm(schedule.end_time) or "",
        break_start_time=_hhmm(schedule.break_start_time),
        break_end_time=_hhmm(schedule.break_end_time),
        slot_duration=schedule.slot_duration,
        max_patients_per_slot=schedule.max_patients_per_slot,
        is_available=schedule.is_available,
        effective_from=schedule.effective_from,
        effective_until=schedule.effective_until,
        notes=schedule.notes,
    )


@router.get("/schedules", response_model=list[ScheduleResponse])
async def list_schedules(doctor: CurrentDoctor, db: DbSession):
    result = await db.execute(
        select(DoctorSchedule, Hospital)
        .join(Hospital, DoctorSchedule.hospital_id == Hospital.id)
        .where(DoctorSchedule.doctor_id == doctor.id)
        .order_by(DoctorSchedule.day_of_week, DoctorSchedule.start_time)
    )
    return [_schedule_to_response(s, h.name) for s, h in result.all()]


def _validate_schedule(payload: ScheduleCreate) -> None:
    if payload.end_time <= payload.start_time:
        raise HTTPException(status_code=422, detail="The sitting must end after it starts")
    if (payload.break_start_time is None) != (payload.break_end_time is None):
        raise HTTPException(status_code=422, detail="A break needs both a start and an end time")
    if payload.break_start_time and payload.break_end_time:
        if payload.break_end_time <= payload.break_start_time:
            raise HTTPException(status_code=422, detail="The break must end after it starts")
        if not (payload.start_time <= payload.break_start_time < payload.end_time):
            raise HTTPException(status_code=422, detail="The break must fall inside the sitting")
    if payload.effective_until and payload.effective_from:
        if payload.effective_until < payload.effective_from:
            raise HTTPException(status_code=422, detail="The end date cannot precede the start date")


@router.post("/schedules", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(payload: ScheduleCreate, doctor: CurrentDoctor, db: DbSession):
    hospital = await db.get(Hospital, payload.hospital_id)
    if hospital is None:
        raise HTTPException(status_code=404, detail="Hospital not found")
    _validate_schedule(payload)

    # A doctor cannot be in two hospitals at once, so overlapping sittings on
    # the same weekday would publish availability that cannot be honoured.
    existing = await db.execute(
        select(DoctorSchedule).where(
            DoctorSchedule.doctor_id == doctor.id,
            DoctorSchedule.day_of_week == payload.day_of_week,
            DoctorSchedule.is_available.is_(True),
        )
    )
    for other in existing.scalars().all():
        if payload.start_time < other.end_time and other.start_time < payload.end_time:
            other_hospital = await db.get(Hospital, other.hospital_id)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This overlaps your {DAY_NAMES[other.day_of_week]} sitting at "
                    f"{other_hospital.name if other_hospital else 'another hospital'} "
                    f"({_hhmm(other.start_time)}-{_hhmm(other.end_time)})"
                ),
            )

    schedule = DoctorSchedule(doctor_id=doctor.id, **payload.model_dump())
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)
    return _schedule_to_response(schedule, hospital.name)


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: int, doctor: CurrentDoctor, db: DbSession):
    schedule = await db.get(DoctorSchedule, schedule_id)
    if schedule is None or schedule.doctor_id != doctor.id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await db.delete(schedule)
    await db.commit()


# --------------------------------------------------------------------------
# Leaves
# --------------------------------------------------------------------------
@router.get("/leaves", response_model=list[LeaveResponse])
async def list_leaves(doctor: CurrentDoctor, db: DbSession):
    result = await db.execute(
        select(DoctorLeave).where(DoctorLeave.doctor_id == doctor.id).order_by(DoctorLeave.start_date.desc())
    )
    payload = []
    for leave in result.scalars().all():
        hospital = await db.get(Hospital, leave.hospital_id) if leave.hospital_id else None
        payload.append(
            LeaveResponse(
                leave_id=leave.id,
                hospital_id=leave.hospital_id,
                hospital_name=hospital.name if hospital else None,
                leave_type=leave.leave_type.value,
                start_date=leave.start_date,
                end_date=leave.end_date,
                start_time=_hhmm(leave.start_time),
                end_time=_hhmm(leave.end_time),
                reason=leave.reason,
                is_approved=leave.is_approved,
                approved_by=leave.approved_by,
                created_at=leave.created_at,
            )
        )
    return payload


@router.post("/leaves", response_model=LeaveResponse, status_code=status.HTTP_201_CREATED)
async def create_leave(payload: LeaveCreate, doctor: CurrentDoctor, db: DbSession):
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=422, detail="Leave cannot end before it starts")
    if (payload.start_time is None) != (payload.end_time is None):
        raise HTTPException(status_code=422, detail="A part-day leave needs both a start and an end time")

    hospital = None
    if payload.hospital_id is not None:
        hospital = await db.get(Hospital, payload.hospital_id)
        if hospital is None:
            raise HTTPException(status_code=404, detail="Hospital not found")

    leave = DoctorLeave(doctor_id=doctor.id, **payload.model_dump())
    db.add(leave)
    await db.commit()
    await db.refresh(leave)

    return LeaveResponse(
        leave_id=leave.id,
        hospital_id=leave.hospital_id,
        hospital_name=hospital.name if hospital else None,
        leave_type=leave.leave_type.value,
        start_date=leave.start_date,
        end_date=leave.end_date,
        start_time=_hhmm(leave.start_time),
        end_time=_hhmm(leave.end_time),
        reason=leave.reason,
        is_approved=leave.is_approved,
        approved_by=leave.approved_by,
        created_at=leave.created_at,
    )


@router.delete("/leaves/{leave_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_leave(leave_id: int, doctor: CurrentDoctor, db: DbSession):
    leave = await db.get(DoctorLeave, leave_id)
    if leave is None or leave.doctor_id != doctor.id:
        raise HTTPException(status_code=404, detail="Leave not found")
    await db.delete(leave)
    await db.commit()


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------
@router.get("/stats", response_model=DoctorStatsResponse)
async def doctor_stats(doctor: CurrentDoctor, db: DbSession):
    """Real numbers for the doctor dashboard, replacing its hardcoded mocks."""
    today = date.today()

    today_consultations = int(
        (
            await db.execute(
                select(func.count(QueueEntry.id)).where(
                    QueueEntry.doctor_id == doctor.id,
                    QueueEntry.status == QueueStatus.COMPLETED,
                    func.date(QueueEntry.consultation_completed_at) == today,
                )
            )
        ).scalar_one()
    )

    current_queue_size = int(
        (
            await db.execute(
                select(func.count(QueueEntry.id)).where(
                    QueueEntry.doctor_id == doctor.id,
                    QueueEntry.status == QueueStatus.WAITING,
                )
            )
        ).scalar_one()
    )

    active_prescriptions = int(
        (
            await db.execute(
                select(func.count(Prescription.id)).where(
                    Prescription.doctor_id == doctor.id,
                    Prescription.status == PrescriptionStatus.ACTIVE,
                )
            )
        ).scalar_one()
    )

    pending_reports = int(
        (
            await db.execute(
                select(func.count(LabReport.id)).where(
                    LabReport.requested_by_doctor_id == doctor.id,
                    LabReport.status.in_([TestStatus.REQUESTED, TestStatus.IN_PROGRESS]),
                )
            )
        ).scalar_one()
    )

    total_patients = int(
        (
            await db.execute(
                select(func.count(func.distinct(MedicalRecord.patient_id))).where(
                    MedicalRecord.doctor_id == doctor.id
                )
            )
        ).scalar_one()
    )

    # The product's own headline: time patients did not spend in a waiting
    # room, because they were told when to leave instead.
    avg = doctor.avg_consultation_minutes or 12.0
    wait_saved = int(today_consultations * avg)

    upcoming: list[UpcomingScheduleEntry] = []
    schedules = await db.execute(
        select(DoctorSchedule, Hospital)
        .join(Hospital, DoctorSchedule.hospital_id == Hospital.id)
        .where(
            DoctorSchedule.doctor_id == doctor.id,
            DoctorSchedule.day_of_week == today.weekday(),
            DoctorSchedule.is_available.is_(True),
        )
        .order_by(DoctorSchedule.start_time)
    )
    sizes = await queue_sizes(db)
    for schedule, hospital in schedules.all():
        upcoming.append(
            UpcomingScheduleEntry(
                hospital_name=hospital.name,
                start_time=_hhmm(schedule.start_time) or "",
                end_time=_hhmm(schedule.end_time) or "",
                patient_count=sizes.get((doctor.id, hospital.id), 0),
            )
        )

    return DoctorStatsResponse(
        today_consultations=today_consultations,
        current_queue_size=current_queue_size,
        avg_consultation_time=round(avg, 1),
        total_wait_time_saved=wait_saved,
        patient_satisfaction=None,  # No feedback mechanism exists yet -- do not invent one.
        upcoming_schedule=upcoming,
        pending_reports=pending_reports,
        active_prescriptions=active_prescriptions,
        total_patients=total_patients,
    )


pharmacy_router = APIRouter(prefix="/api/pharmacy", tags=["Pharmacy"])


@pharmacy_router.get("/stats", response_model=PharmacyStatsResponse)
async def pharmacy_stats(pharmacy: CurrentPharmacy, db: DbSession):
    """Real numbers for the pharmacy dashboard, replacing its mockStats block."""
    today = date.today()

    total = int(
        (
            await db.execute(
                select(func.count(Prescription.id)).where(
                    Prescription.dispensed_by_pharmacy_id == pharmacy.id
                )
            )
        ).scalar_one()
    )
    today_dispensed = int(
        (
            await db.execute(
                select(func.count(Prescription.id)).where(
                    Prescription.dispensed_by_pharmacy_id == pharmacy.id,
                    func.date(Prescription.dispensed_at) == today,
                )
            )
        ).scalar_one()
    )
    pending = int(
        (
            await db.execute(
                select(func.count(Prescription.id)).where(Prescription.status == PrescriptionStatus.ACTIVE)
            )
        ).scalar_one()
    )

    # Sum the actual line items rather than guessing an average basket.
    from src.models.clinical import Medicine, PrescriptionMedicine

    revenue = (
        await db.execute(
            select(func.sum(PrescriptionMedicine.quantity_prescribed * Medicine.price_per_unit))
            .join(Medicine, PrescriptionMedicine.medicine_id == Medicine.id)
            .join(Prescription, PrescriptionMedicine.prescription_id == Prescription.id)
            .where(Prescription.dispensed_by_pharmacy_id == pharmacy.id)
        )
    ).scalar()

    return PharmacyStatsResponse(
        total_prescriptions=total,
        today_dispensed=today_dispensed,
        pending_dispensing=pending,
        total_revenue=round(float(revenue or 0.0), 2),
    )
