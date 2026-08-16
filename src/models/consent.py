from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, TimestampMixin
from src.models.enums import ConsentStatus, ConsentType, DocumentStatus, DocumentType


def _enum(enum_cls, length: int):
    return Enum(enum_cls, native_enum=False, length=length, values_callable=lambda e: [m.value for m in e])


class PatientConsent(Base, TimestampMixin):
    """What a patient has agreed to share, with whom, and for how long.

    Per-category rather than all-or-nothing: a patient can show a doctor their
    prescriptions without opening their psychiatric history. The server filters
    every clinical read against this -- the client only requesting permitted
    tabs is a convenience, not the control.
    """

    __tablename__ = "patient_consents"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)

    # Scope. At least one must be set; all three may narrow together.
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), index=True)
    hospital_id: Mapped[int | None] = mapped_column(ForeignKey("hospitals.id"), index=True)
    consultation_id: Mapped[int | None] = mapped_column(
        ForeignKey("queue_entries.id", ondelete="CASCADE"), index=True
    )

    consent_type: Mapped[ConsentType] = mapped_column(_enum(ConsentType, 32), nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)

    allow_medical_history: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_lab_reports: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_prescriptions: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_uploaded_documents: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_allergies: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_chronic_conditions: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_emergency_contact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Narrower still: only these document types, minus these specific documents.
    allowed_document_types: Mapped[list | None] = mapped_column(JSON)
    excluded_document_ids: Mapped[list | None] = mapped_column(JSON)

    status: Mapped[ConsentStatus] = mapped_column(
        _enum(ConsentStatus, 16), default=ConsentStatus.GRANTED, nullable=False, index=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    patient = relationship("Patient")

    def allows(self, category: str) -> bool:
        return bool(getattr(self, f"allow_{category}", False))


class MedicalDocument(Base, TimestampMixin):
    """A file the patient uploaded. Bytes live in object storage, not the DB."""

    __tablename__ = "medical_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)

    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Object key in the bucket. Never exposed to clients -- they get a
    # short-lived presigned URL instead.
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)

    document_type: Mapped[DocumentType] = mapped_column(_enum(DocumentType, 32), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    document_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    doctor_name: Mapped[str | None] = mapped_column(String(255))
    hospital_name: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[DocumentStatus] = mapped_column(
        _enum(DocumentStatus, 16), default=DocumentStatus.UPLOADED, nullable=False
    )

    patient = relationship("Patient")


class ConsentAuditLog(Base):
    """Who read what, when, and under which consent.

    A patient who cannot see who opened their record has not really been given
    control of it. Written on every consent-gated read, including denials.
    """

    __tablename__ = "consent_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    consent_id: Mapped[int | None] = mapped_column(ForeignKey("patient_consents.id"))
    queue_entry_id: Mapped[int | None] = mapped_column(ForeignKey("queue_entries.id", ondelete="SET NULL"))

    category: Mapped[str] = mapped_column(String(64), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
