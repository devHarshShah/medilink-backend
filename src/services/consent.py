"""Consent resolution and enforcement.

Every clinical read a doctor makes about a patient passes through here. The
frontend only requests the tabs it thinks are permitted, but that is a
convenience -- this module is the control, and it assumes the client is lying.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.consent import ConsentAuditLog, PatientConsent
from src.models.enums import ConsentStatus, QueueStatus
from src.models.queue import QueueEntry

logger = logging.getLogger(__name__)

CATEGORIES = (
    "medical_history",
    "lab_reports",
    "prescriptions",
    "uploaded_documents",
    "allergies",
    "chronic_conditions",
    "emergency_contact",
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def is_live(consent: PatientConsent) -> bool:
    if consent.status is not ConsentStatus.GRANTED:
        return False
    expires = _aware(consent.expires_at)
    return expires is None or expires > datetime.now(UTC)


async def expire_stale(db: AsyncSession, consents: list[PatientConsent]) -> None:
    """Flip lapsed grants to EXPIRED so the patient's own list tells the truth."""
    changed = False
    now = datetime.now(UTC)
    for consent in consents:
        expires = _aware(consent.expires_at)
        if consent.status is ConsentStatus.GRANTED and expires is not None and expires <= now:
            consent.status = ConsentStatus.EXPIRED
            changed = True
    if changed:
        await db.commit()


async def resolve_for_consultation(
    db: AsyncSession, entry: QueueEntry, doctor_id: int
) -> PatientConsent | None:
    """The consent covering this doctor for this consultation, if any.

    Preference order: a grant scoped to this exact consultation, then this
    doctor, then this hospital, then an unscoped ongoing-care grant. The
    narrowest match wins so a broad standing consent cannot silently widen a
    deliberately narrow one.
    """
    result = await db.execute(
        select(PatientConsent).where(
            PatientConsent.patient_id == entry.patient_id,
            PatientConsent.status == ConsentStatus.GRANTED,
            or_(
                PatientConsent.consultation_id == entry.id,
                PatientConsent.doctor_id == doctor_id,
                PatientConsent.hospital_id == entry.hospital_id,
                (
                    PatientConsent.doctor_id.is_(None)
                    & PatientConsent.hospital_id.is_(None)
                    & PatientConsent.consultation_id.is_(None)
                ),
            ),
        )
    )
    candidates = [c for c in result.scalars().all() if is_live(c)]
    if not candidates:
        return None

    def specificity(consent: PatientConsent) -> int:
        if consent.consultation_id == entry.id:
            return 0
        if consent.doctor_id == doctor_id:
            return 1
        if consent.hospital_id == entry.hospital_id:
            return 2
        return 3

    candidates.sort(key=lambda c: (specificity(c), -c.id))
    return candidates[0]


async def audit(
    db: AsyncSession,
    patient_id: int,
    actor_user_id: int | None,
    category: str,
    granted: bool,
    consent_id: int | None = None,
    queue_entry_id: int | None = None,
    reason: str | None = None,
) -> None:
    """Record the access attempt. Best-effort: a failure to log must not deny a
    doctor legitimate access mid-consultation."""
    try:
        db.add(
            ConsentAuditLog(
                patient_id=patient_id,
                actor_user_id=actor_user_id,
                consent_id=consent_id,
                queue_entry_id=queue_entry_id,
                category=category,
                granted=granted,
                reason=reason,
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("Could not write consent audit entry", exc_info=True)


class ConsentDenied(Exception):
    """Raised when a category is not covered. Carries a patient-facing reason."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


async def require(
    db: AsyncSession,
    entry: QueueEntry,
    doctor_id: int,
    actor_user_id: int | None,
    category: str,
) -> PatientConsent:
    """Authorise one category of clinical read, or raise ConsentDenied.

    Two gates, both required:
      1. The doctor must be the one actually treating this patient, and the
         consultation must still be live. A finished consultation does not
         leave a standing key to someone's record.
      2. The patient must have granted this specific category.
    """
    if entry.doctor_id != doctor_id:
        await audit(db, entry.patient_id, actor_user_id, category, False, reason="not_treating_doctor")
        raise ConsentDenied("This patient is not in your queue")

    if entry.status not in (QueueStatus.WAITING, QueueStatus.IN_CONSULTATION):
        await audit(
            db,
            entry.patient_id,
            actor_user_id,
            category,
            False,
            queue_entry_id=entry.id,
            reason="consultation_not_active",
        )
        raise ConsentDenied("This consultation has ended and the record is no longer available")

    consent = await resolve_for_consultation(db, entry, doctor_id)
    if consent is None:
        await audit(
            db,
            entry.patient_id,
            actor_user_id,
            category,
            False,
            queue_entry_id=entry.id,
            reason="no_consent",
        )
        raise ConsentDenied("The patient has not shared their records for this consultation")

    if not consent.allows(category):
        await audit(
            db,
            entry.patient_id,
            actor_user_id,
            category,
            False,
            consent_id=consent.id,
            queue_entry_id=entry.id,
            reason="category_not_permitted",
        )
        raise ConsentDenied(
            f"The patient has not shared their {category.replace('_', ' ')} for this consultation"
        )

    await audit(
        db,
        entry.patient_id,
        actor_user_id,
        category,
        True,
        consent_id=consent.id,
        queue_entry_id=entry.id,
    )
    return consent


def filter_documents(consent: PatientConsent, documents: list) -> list:
    """Apply document-level narrowing on top of the category flag."""
    allowed_types = set(consent.allowed_document_types or [])
    excluded_ids = set(consent.excluded_document_ids or [])
    result = []
    for document in documents:
        if document.id in excluded_ids:
            continue
        if allowed_types and document.document_type.value not in allowed_types:
            continue
        result.append(document)
    return result
