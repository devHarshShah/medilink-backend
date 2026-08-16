from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.models.enums import UrgencyLevel


class TriageAnalysis(Base):
    """Every triage run, stored for tuning and for attaching to a queue entry.

    Kept even when the patient abandons the booking flow -- the interesting
    cases for tuning the rule engine are often the ones nobody completed.
    """

    __tablename__ = "triage_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id", ondelete="SET NULL"), index=True)

    symptoms_description: Mapped[str] = mapped_column(Text, nullable=False)
    patient_age: Mapped[int | None] = mapped_column(Integer)
    patient_gender: Mapped[str | None] = mapped_column(String(16))
    medical_history: Mapped[str | None] = mapped_column(Text)

    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    model: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    urgency_level: Mapped[UrgencyLevel] = mapped_column(
        Enum(UrgencyLevel, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
    )
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Full SymptomsAnalysisResponse, so a queue entry can replay exactly what
    # the patient was shown at the time they joined.
    response: Mapped[dict] = mapped_column(JSON, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
