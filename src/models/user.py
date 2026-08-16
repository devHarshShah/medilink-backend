from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, TimestampMixin
from src.models.enums import UserRole


class User(Base, TimestampMixin):
    """Single identity table. Every role-specific profile (patient, doctor, pharmacy,
    ...) hangs off exactly one User, which owns the credentials."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
        default=UserRole.PATIENT,
        nullable=False,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def is_superuser(self) -> bool:
        """Present in the original spec's /api/v1/users response. Derived from the
        role rather than stored, so it can never drift out of sync with it."""
        return self.role == UserRole.ADMIN

    patient = relationship("Patient", back_populates="user", uselist=False)
    doctor = relationship("Doctor", back_populates="user", uselist=False)
    nurse = relationship("Nurse", back_populates="user", uselist=False)
    pharmacy = relationship("Pharmacy", back_populates="user", uselist=False)
    diagnostic_center = relationship("DiagnosticCenter", back_populates="user", uselist=False)
