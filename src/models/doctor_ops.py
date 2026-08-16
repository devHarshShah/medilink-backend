from datetime import date, datetime, time

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Text, Time, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, TimestampMixin
from src.models.enums import DoctorAvailabilityStatus, LeaveType


def _enum(enum_cls, length: int):
    return Enum(enum_cls, native_enum=False, length=length, values_callable=lambda e: [m.value for m in e])


class DoctorCheckIn(Base, TimestampMixin):
    """A doctor's presence at one hospital for one sitting.

    A doctor consults at several hospitals across a week, so availability is a
    live fact rather than a property of their profile. The queue engine reads
    open check-ins to decide where a doctor can actually be seen.
    """

    __tablename__ = "doctor_checkins"

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id", ondelete="CASCADE"), index=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"), index=True)

    check_in_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Null means still present. This is the flag the queue engine filters on.
    check_out_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    presence_status: Mapped[DoctorAvailabilityStatus] = mapped_column(
        _enum(DoctorAvailabilityStatus, 16),
        default=DoctorAvailabilityStatus.AVAILABLE,
        nullable=False,
    )
    last_activity: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    notes: Mapped[str | None] = mapped_column(Text)
    # Where the doctor said they were heading next, for the hand-off view.
    next_hospital_id: Mapped[int | None] = mapped_column(ForeignKey("hospitals.id"))

    doctor = relationship("Doctor")
    hospital = relationship("Hospital", foreign_keys=[hospital_id])

    @property
    def is_open(self) -> bool:
        return self.check_out_time is None


class DoctorSchedule(Base, TimestampMixin):
    """A recurring weekly sitting at one hospital."""

    __tablename__ = "doctor_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id", ondelete="CASCADE"), index=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"), index=True)

    # 0 = Monday, matching Python's date.weekday().
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    break_start_time: Mapped[time | None] = mapped_column(Time)
    break_end_time: Mapped[time | None] = mapped_column(Time)

    slot_duration: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    max_patients_per_slot: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_until: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    doctor = relationship("Doctor")
    hospital = relationship("Hospital")


class DoctorLeave(Base, TimestampMixin):
    """Planned absence. Overrides the weekly schedule."""

    __tablename__ = "doctor_leaves"

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id", ondelete="CASCADE"), index=True)
    # Null means every hospital.
    hospital_id: Mapped[int | None] = mapped_column(ForeignKey("hospitals.id"))

    leave_type: Mapped[LeaveType] = mapped_column(_enum(LeaveType, 20), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Set for a part-day absence; null means the whole day.
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)

    reason: Mapped[str | None] = mapped_column(Text)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    doctor = relationship("Doctor")
    hospital = relationship("Hospital")
