from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, TimestampMixin
from src.models.enums import PrescriptionStatus, TestStatus


class MedicalRecord(Base, TimestampMixin):
    __tablename__ = "medical_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)

    chief_complaint: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis: Mapped[str] = mapped_column(Text, nullable=False)
    treatment_plan: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    vitals: Mapped[str | None] = mapped_column(Text)
    visit_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    patient = relationship("Patient")
    doctor = relationship("Doctor")


class Medicine(Base):
    __tablename__ = "medicines"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    generic_name: Mapped[str | None] = mapped_column(String(255), index=True)
    manufacturer: Mapped[str] = mapped_column(String(255), nullable=False, default="Unknown")
    strength: Mapped[str] = mapped_column(String(60), nullable=False, default="N/A")
    form: Mapped[str] = mapped_column(String(60), nullable=False, default="tablet")
    price_per_unit: Mapped[float | None] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Prescription(Base, TimestampMixin):
    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)

    prescription_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[PrescriptionStatus] = mapped_column(
        Enum(
            PrescriptionStatus,
            native_enum=False,
            length=20,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=PrescriptionStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text)
    follow_up_date: Mapped[date | None] = mapped_column(Date)

    # Dispensing audit - set when a pharmacy scans the QR and hands over the medicine.
    dispensed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispensed_by_pharmacy_id: Mapped[int | None] = mapped_column(ForeignKey("pharmacies.id"))

    patient = relationship("Patient")
    doctor = relationship("Doctor")
    medicines = relationship(
        "PrescriptionMedicine", back_populates="prescription", cascade="all, delete-orphan"
    )


class PrescriptionMedicine(Base):
    __tablename__ = "prescription_medicines"

    id: Mapped[int] = mapped_column(primary_key=True)
    prescription_id: Mapped[int] = mapped_column(
        ForeignKey("prescriptions.id", ondelete="CASCADE"), index=True
    )
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id"))

    dosage: Mapped[str] = mapped_column(String(120), nullable=False)
    frequency: Mapped[str] = mapped_column(String(120), nullable=False)
    duration: Mapped[str] = mapped_column(String(120), nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text)
    quantity_prescribed: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    prescription = relationship("Prescription", back_populates="medicines")
    medicine = relationship("Medicine", lazy="joined")


class LabReport(Base, TimestampMixin):
    __tablename__ = "lab_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    diagnostic_center_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_centers.id"), index=True)
    # Set when a doctor orders the test during a consultation (phase 6).
    requested_by_doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"))

    test_name: Mapped[str] = mapped_column(String(255), nullable=False)
    test_type: Mapped[str] = mapped_column(String(120), nullable=False)
    results: Mapped[str | None] = mapped_column(Text)
    reference_ranges: Mapped[str | None] = mapped_column(Text)
    interpretation: Mapped[str | None] = mapped_column(Text)
    technician_notes: Mapped[str | None] = mapped_column(Text)

    requested_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[TestStatus] = mapped_column(
        Enum(TestStatus, native_enum=False, length=20, values_callable=lambda e: [m.value for m in e]),
        default=TestStatus.REQUESTED,
        nullable=False,
        index=True,
    )
    report_file_path: Mapped[str | None] = mapped_column(String(512))

    patient = relationship("Patient")
