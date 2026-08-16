"""Patient document vault and consent management.

Both hang off /api/patients/me -- these are the patient's own controls over
their record.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from src.core.config import settings
from src.core.deps import CurrentPatient, DbSession
from src.models.consent import MedicalDocument, PatientConsent
from src.models.enums import ConsentStatus, DocumentStatus, DocumentType
from src.models.organizations import Hospital
from src.models.people import Doctor
from src.models.queue import QueueEntry
from src.models.user import User
from src.schemas.consent import (
    ConsentCreate,
    ConsentResponse,
    MedicalDocumentResponse,
)
from src.services import storage
from src.services.consent import expire_stale

router = APIRouter(prefix="/api/patients/me", tags=["Patients"])


def _document_to_response(document: MedicalDocument) -> MedicalDocumentResponse:
    return MedicalDocumentResponse(
        id=document.id,
        file_name=document.file_name,
        document_type=document.document_type.value,
        description=document.description,
        document_date=document.document_date,
        doctor_name=document.doctor_name,
        hospital_name=document.hospital_name,
        status=document.status.value,
        file_size=document.file_size,
        created_at=document.created_at,
    )


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------
@router.get("/documents", response_model=list[MedicalDocumentResponse])
async def list_documents(patient: CurrentPatient, db: DbSession):
    result = await db.execute(
        select(MedicalDocument)
        .where(MedicalDocument.patient_id == patient.id)
        .order_by(MedicalDocument.created_at.desc())
    )
    return [_document_to_response(d) for d in result.scalars().all()]


@router.post("/documents/upload", response_model=MedicalDocumentResponse, status_code=201)
async def upload_document(
    patient: CurrentPatient,
    db: DbSession,
    file: UploadFile = File(...),
    document_type: str = Form(...),
    description: str | None = Form(None),
    document_date: str | None = Form(None),
    doctor_name: str | None = Form(None),
    hospital_name: str | None = Form(None),
):
    try:
        doc_type = DocumentType(document_type)
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"'{document_type}' is not a recognised document type"
        ) from None

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="The file is empty")
    if len(data) > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"Files must be {limit_mb}MB or smaller")

    # Sniff the real type from the bytes. The browser-supplied content type is
    # a claim -- renaming a script to .pdf must not get it stored as one.
    sniffed = storage.sniff_content_type(data[:16])
    if sniffed is None or sniffed not in settings.allowed_upload_types:
        raise HTTPException(status_code=422, detail="Only JPEG, PNG and PDF files can be uploaded")

    parsed_date = None
    if document_date:
        try:
            parsed_date = datetime.fromisoformat(document_date.replace("Z", "+00:00"))
            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="document_date must be an ISO date") from None

    key = storage.build_key(patient.id, file.filename or "document")
    try:
        storage.upload(key, data, sniffed)
    except storage.StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    document = MedicalDocument(
        patient_id=patient.id,
        file_name=(file.filename or "document")[:255],
        storage_key=key,
        content_type=sniffed,
        file_size=len(data),
        document_type=doc_type,
        description=description,
        document_date=parsed_date,
        doctor_name=doctor_name,
        hospital_name=hospital_name,
        status=DocumentStatus.UPLOADED,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)
    return _document_to_response(document)


@router.get("/documents/{document_id}/download")
async def download_document(document_id: int, patient: CurrentPatient, db: DbSession):
    """Redirect to a short-lived presigned URL.

    The object key is never exposed -- the link authorises itself and expires,
    because it will end up in browser history.
    """
    document = await db.get(MedicalDocument, document_id)
    if document is None or document.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="Document not found")

    try:
        url = storage.presigned_url(document.storage_key, document.file_name)
    except storage.StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: int, patient: CurrentPatient, db: DbSession):
    document = await db.get(MedicalDocument, document_id)
    if document is None or document.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="Document not found")

    key = document.storage_key
    await db.delete(document)
    await db.commit()
    # The row is the source of truth for what anyone can see; an orphaned
    # object is a cleanup problem, not a privacy one.
    storage.delete(key)


# --------------------------------------------------------------------------
# Consents
# --------------------------------------------------------------------------
async def _consent_to_response(db: DbSession, consent: PatientConsent) -> ConsentResponse:
    doctor_name = None
    if consent.doctor_id:
        doctor = await db.get(Doctor, consent.doctor_id)
        if doctor:
            user = await db.get(User, doctor.user_id)
            doctor_name = user.full_name if user else None
    hospital_name = None
    if consent.hospital_id:
        hospital = await db.get(Hospital, consent.hospital_id)
        hospital_name = hospital.name if hospital else None

    return ConsentResponse(
        id=consent.id,
        patient_id=consent.patient_id,
        doctor_id=consent.doctor_id,
        doctor_name=doctor_name,
        hospital_id=consent.hospital_id,
        hospital_name=hospital_name,
        consultation_id=consent.consultation_id,
        consent_type=consent.consent_type.value,
        purpose=consent.purpose,
        allow_medical_history=consent.allow_medical_history,
        allow_lab_reports=consent.allow_lab_reports,
        allow_prescriptions=consent.allow_prescriptions,
        allow_uploaded_documents=consent.allow_uploaded_documents,
        allow_allergies=consent.allow_allergies,
        allow_chronic_conditions=consent.allow_chronic_conditions,
        allow_emergency_contact=consent.allow_emergency_contact,
        allowed_document_types=consent.allowed_document_types,
        excluded_document_ids=consent.excluded_document_ids,
        status=consent.status.value,
        granted_at=consent.granted_at,
        expires_at=consent.expires_at,
        revoked_at=consent.revoked_at,
        notes=consent.notes,
    )


@router.get("/consents", response_model=list[ConsentResponse])
async def list_consents(patient: CurrentPatient, db: DbSession):
    result = await db.execute(
        select(PatientConsent)
        .where(PatientConsent.patient_id == patient.id)
        .order_by(PatientConsent.granted_at.desc())
    )
    consents = list(result.scalars().all())
    # Lapsed grants are flipped on read so the patient's own list tells the
    # truth about what is still live.
    await expire_stale(db, consents)
    return [await _consent_to_response(db, c) for c in consents]


@router.post("/consents", response_model=ConsentResponse, status_code=status.HTTP_201_CREATED)
async def create_consent(payload: ConsentCreate, patient: CurrentPatient, db: DbSession):
    if payload.doctor_id is not None and await db.get(Doctor, payload.doctor_id) is None:
        raise HTTPException(status_code=404, detail="Doctor not found")
    if payload.hospital_id is not None and await db.get(Hospital, payload.hospital_id) is None:
        raise HTTPException(status_code=404, detail="Hospital not found")

    if payload.consultation_id is not None:
        entry = await db.get(QueueEntry, payload.consultation_id)
        if entry is None or entry.patient_id != patient.id:
            raise HTTPException(status_code=404, detail="Consultation not found")

    if payload.expires_at is not None:
        expires = payload.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            raise HTTPException(status_code=422, detail="The expiry time must be in the future")

    consent = PatientConsent(
        patient_id=patient.id,
        status=ConsentStatus.GRANTED,
        **payload.model_dump(),
    )
    db.add(consent)
    await db.commit()
    await db.refresh(consent)
    return await _consent_to_response(db, consent)


@router.put("/consents/{consent_id}/revoke", response_model=ConsentResponse)
async def revoke_consent(consent_id: int, patient: CurrentPatient, db: DbSession):
    consent = await db.get(PatientConsent, consent_id)
    if consent is None or consent.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="Consent not found")
    if consent.status is ConsentStatus.REVOKED:
        raise HTTPException(status_code=409, detail="This consent has already been revoked")

    consent.status = ConsentStatus.REVOKED
    consent.revoked_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(consent)
    return await _consent_to_response(db, consent)
