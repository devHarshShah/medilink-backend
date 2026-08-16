"""QR generation and grant validation."""

import base64
import io
import random
from datetime import UTC, datetime, timedelta

import qrcode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import generate_access_token_string
from src.models.access import QRAccessGrant
from src.models.enums import AccessType
from src.models.people import Patient

# Which record categories each access type unlocks. Enforced server-side on every
# QR-consuming endpoint - the client is never trusted to self-limit.
ACCESS_TYPE_SCOPES: dict[AccessType, set[str]] = {
    AccessType.FULL_ACCESS: {"profile", "records", "prescriptions", "lab_reports", "documents"},
    AccessType.PRESCRIPTION_ONLY: {"profile", "prescriptions"},
    AccessType.REPORTS_ONLY: {"profile", "lab_reports"},
    AccessType.EMERGENCY_ACCESS: {"profile", "records", "prescriptions", "lab_reports"},
}


def render_qr_base64(payload: str) -> str:
    """Return a base64-encoded PNG with no data-URI prefix.

    The frontend builds `data:image/png;base64,${qr_code}` itself, so adding the
    prefix here would produce a doubled, broken src.
    """
    qr = qrcode.QRCode(version=None, box_size=8, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


async def create_qr_grant(
    db: AsyncSession,
    patient: Patient,
    access_type: AccessType,
    ttl_minutes: int,
) -> tuple[QRAccessGrant, str]:
    token = generate_access_token_string()
    grant = QRAccessGrant(
        patient_id=patient.id,
        access_token=token,
        access_type=access_type,
        expires_at=datetime.now(UTC) + timedelta(minutes=ttl_minutes),
    )
    db.add(grant)
    await db.commit()
    await db.refresh(grant)

    qr_code = render_qr_base64(token)
    return grant, qr_code


async def resolve_grant(
    db: AsyncSession,
    access_token: str,
    required_scope: str | None = None,
) -> tuple[QRAccessGrant, Patient]:
    """Validate a QR token and return the grant plus its patient.

    Raises ValueError with a user-facing message; callers translate to HTTP.
    """
    result = await db.execute(select(QRAccessGrant).where(QRAccessGrant.access_token == access_token))
    grant = result.scalar_one_or_none()
    if grant is None:
        raise ValueError("Invalid access code")
    if not grant.is_valid:
        raise ValueError("This access code has expired or been revoked")

    if required_scope and required_scope not in ACCESS_TYPE_SCOPES[grant.access_type]:
        raise ValueError(f"This access code does not grant access to {required_scope.replace('_', ' ')}")

    patient = await db.get(Patient, grant.patient_id)
    if patient is None:
        raise ValueError("Patient not found")

    grant.use_count += 1
    grant.last_used_at = datetime.now(UTC)
    await db.commit()

    return grant, patient


async def generate_uhid(db: AsyncSession) -> str:
    """UHID format: ML + YY + 8 digits. Retries on the (very unlikely) collision."""
    year = datetime.now(UTC).strftime("%y")
    for _ in range(10):
        candidate = f"ML{year}{random.randint(0, 99_999_999):08d}"
        existing = await db.execute(select(Patient.id).where(Patient.uhid == candidate))
        if existing.scalar_one_or_none() is None:
            return candidate
    raise RuntimeError("Could not allocate a unique UHID")
