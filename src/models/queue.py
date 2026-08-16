from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, TimestampMixin
from src.models.enums import QueuePriority, QueueStatus, QueueType


def _enum(enum_cls, length: int):
    return Enum(enum_cls, native_enum=False, length=length, values_callable=lambda e: [m.value for m in e])


class QueueEntry(Base, TimestampMixin):
    """A patient's place in a doctor's queue at a specific hospital.

    In a cross-hospital queue the patient names candidate doctors and the server
    picks the (doctor x hospital) pair with the lowest predicted total time. A
    background re-scorer may move a waiting patient to a better centre until
    lock-in, after which the slot is frozen so nobody is re-routed while already
    travelling.
    """

    __tablename__ = "queue_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)

    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"), index=True)

    queue_type: Mapped[QueueType] = mapped_column(
        _enum(QueueType, 24), default=QueueType.HOSPITAL_SPECIFIC, nullable=False
    )
    # Per (doctor, hospital, day). Displayed to the patient as their token.
    queue_number: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[QueueStatus] = mapped_column(
        _enum(QueueStatus, 20), default=QueueStatus.WAITING, nullable=False, index=True
    )
    priority: Mapped[QueuePriority] = mapped_column(
        _enum(QueuePriority, 16), default=QueuePriority.NORMAL, nullable=False
    )
    is_emergency: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    symptoms_description: Mapped[str | None] = mapped_column(Text)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    # The full SymptomsAnalysisResponse the patient was shown when they joined.
    ai_recommendation: Mapped[dict | None] = mapped_column(JSON)
    # Candidates the patient offered; the re-scorer may only move between these.
    candidate_doctor_ids: Mapped[list | None] = mapped_column(JSON)

    patient_latitude: Mapped[float | None] = mapped_column(Float)
    patient_longitude: Mapped[float | None] = mapped_column(Float)

    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    consultation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consultation_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    patient_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Set once the patient should be travelling. After this the entry stops
    # floating between centres.
    locked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the patient should set off, from the last scoring pass.
    leave_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    notes: Mapped[str | None] = mapped_column(Text)
    reassignment_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    patient = relationship("Patient")
    doctor = relationship("Doctor")
    hospital = relationship("Hospital")

    @property
    def is_active(self) -> bool:
        return self.status in (QueueStatus.WAITING, QueueStatus.IN_CONSULTATION)
