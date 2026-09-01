from datetime import date

from sqlalchemy import Boolean, Date, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, TimestampMixin
from src.models.enums import Gender


class Patient(Base, TimestampMixin):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)

    # Universal Health ID - the handle every other portal looks a patient up by.
    uhid: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    gender: Mapped[Gender] = mapped_column(
        Enum(Gender, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    blood_group: Mapped[str | None] = mapped_column(String(8))
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    emergency_contact_name: Mapped[str | None] = mapped_column(String(255))
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(32))
    medical_allergies: Mapped[str | None] = mapped_column(Text)
    chronic_conditions: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # The patient's chosen primary doctor. Standing access to shared record
    # categories is carried by an ongoing-care PatientConsent, not this column.
    family_doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), index=True)

    user = relationship("User", back_populates="patient")
    family_doctor = relationship("Doctor", foreign_keys=[family_doctor_id])

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Doctor(Base, TimestampMixin):
    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    # Primary affiliation. A doctor can consult at other hospitals via check-in (phase 4).
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"), index=True)

    license_number: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    specialization: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    years_of_experience: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qualification: Mapped[str] = mapped_column(String(255), nullable=False)
    consultation_fee: Mapped[float | None] = mapped_column(Float)

    # Rolling estimate driven by completed consultations; seeds the queue wait maths.
    avg_consultation_minutes: Mapped[float] = mapped_column(Float, default=12.0, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user = relationship("User", back_populates="doctor")
    hospital = relationship("Hospital", back_populates="doctors")


class Nurse(Base, TimestampMixin):
    __tablename__ = "nurses"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"), index=True)

    license_number: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    department: Mapped[str] = mapped_column(String(120), nullable=False)
    shift: Mapped[str] = mapped_column(String(60), nullable=False)
    years_of_experience: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user = relationship("User", back_populates="nurse")
    hospital = relationship("Hospital", back_populates="nurses")
