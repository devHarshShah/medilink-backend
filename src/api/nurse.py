from datetime import date

from fastapi import APIRouter
from sqlalchemy import func, select

from src.core.deps import CurrentNurse, DbSession
from src.models.doctor_ops import DoctorCheckIn
from src.models.enums import DoctorAvailabilityStatus, QueueStatus
from src.models.queue import QueueEntry
from src.schemas.healthcare import NurseStatsResponse

router = APIRouter(prefix="/api/nurse", tags=["Nurse"])


@router.get("/stats", response_model=NurseStatsResponse)
async def nurse_stats(nurse: CurrentNurse, db: DbSession):
    """The live picture for the nurse's own hospital."""
    today = date.today()
    hospital_id = nurse.hospital_id

    async def _count(*where) -> int:
        stmt = select(func.count()).select_from(QueueEntry).where(QueueEntry.hospital_id == hospital_id)
        for clause in where:
            stmt = stmt.where(clause)
        return int((await db.execute(stmt)).scalar_one())

    doctors_on_duty = int(
        (
            await db.execute(
                select(func.count(func.distinct(DoctorCheckIn.doctor_id))).where(
                    DoctorCheckIn.hospital_id == hospital_id,
                    DoctorCheckIn.check_out_time.is_(None),
                    DoctorCheckIn.presence_status.in_(
                        [DoctorAvailabilityStatus.AVAILABLE, DoctorAvailabilityStatus.BUSY]
                    ),
                )
            )
        ).scalar_one()
    )

    return NurseStatsResponse(
        patients_waiting=await _count(QueueEntry.status == QueueStatus.WAITING),
        in_consultation=await _count(QueueEntry.status == QueueStatus.IN_CONSULTATION),
        emergency_waiting=await _count(
            QueueEntry.status == QueueStatus.WAITING, QueueEntry.is_emergency.is_(True)
        ),
        doctors_on_duty=doctors_on_duty,
        completed_today=await _count(
            QueueEntry.status == QueueStatus.COMPLETED,
            func.date(QueueEntry.consultation_completed_at) == today,
        ),
    )
