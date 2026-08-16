from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, TimestampMixin
from src.models.enums import AccessType


class QRAccessGrant(Base, TimestampMixin):
    """A short-lived, revocable, DB-backed grant issued by a patient.

    Deliberately not a JWT: the patient must be able to revoke it, and every
    consumption is counted so a screenshot of an old QR is not a standing key.
    """

    __tablename__ = "qr_access_grants"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)

    access_token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    access_type: Mapped[AccessType] = mapped_column(
        Enum(AccessType, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    patient = relationship("Patient")

    @property
    def is_valid(self) -> bool:
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return not self.is_revoked and expires > datetime.now(UTC)
