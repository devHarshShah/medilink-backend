from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from src.core.deps import CurrentDoctor, CurrentUser, DbSession
from src.models.clinical import LabReport, MedicalRecord, Medicine, Prescription, PrescriptionMedicine
from src.models.consent import MedicalDocument, PatientConsent
from src.models.enums import ConsentStatus
from src.models.people import Patient
from src.models.user import User
from src.schemas.consent import MedicalDocumentResponse
from src.schemas.healthcare import (
    DoctorResponse,
    LabReportResponse,
    MedicalRecordCreate,
    MedicalRecordResponse,
    PatientResponse,
    PrescriptionCreate,
    PrescriptionResponse,
)
from src.services import consent as consent_service
from src.services import storage

router = APIRouter(prefix="/api/doctor", tags=["Doctor"])


async def _guard_standing(db: DbSession, patient_id: int, doctor, user_id: int, category: str):
    """403 unless the patient has given this doctor standing access to `category`."""
    try:
        return await consent_service.require_standing(db, patient_id, doctor.id, user_id, category)
    except consent_service.ConsentDenied as exc:
        raise HTTPException(status_code=403, detail=exc.message) from exc


@router.get("/me", response_model=DoctorResponse)
async def get_my_doctor_profile(doctor: CurrentDoctor, db: DbSession):
    """The frontend reads `.id` off this to address every queue endpoint."""
    user = await db.get(User, doctor.user_id)
    item = DoctorResponse.model_validate(doctor)
    if user is not None:
        item.full_name = user.full_name
        item.email = user.email
    return item


@router.get("/patients/{uhid}", response_model=PatientResponse)
async def get_patient(uhid: str, doctor: CurrentDoctor, db: DbSession):
    result = await db.execute(select(Patient).where(Patient.uhid == uhid.upper()))
    patient = result.scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="No patient found with that UHID")
    return PatientResponse.model_validate(patient)


@router.post("/medical-records", response_model=MedicalRecordResponse, status_code=status.HTTP_201_CREATED)
async def create_medical_record(payload: MedicalRecordCreate, doctor: CurrentDoctor, db: DbSession):
    if await db.get(Patient, payload.patient_id) is None:
        raise HTTPException(status_code=404, detail="Patient not found")

    record = MedicalRecord(**payload.model_dump(), doctor_id=doctor.id)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return MedicalRecordResponse.model_validate(record)


@router.get("/medical-records/patient/{patient_id}", response_model=list[MedicalRecordResponse])
async def get_patient_medical_records(
    patient_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession
):
    await _guard_standing(db, patient_id, doctor, user.id, "medical_history")
    result = await db.execute(
        select(MedicalRecord)
        .where(MedicalRecord.patient_id == patient_id)
        .order_by(MedicalRecord.visit_date.desc())
    )
    return [MedicalRecordResponse.model_validate(r) for r in result.scalars().all()]


@router.post("/prescriptions", response_model=PrescriptionResponse, status_code=status.HTTP_201_CREATED)
async def create_prescription(payload: PrescriptionCreate, doctor: CurrentDoctor, db: DbSession):
    if await db.get(Patient, payload.patient_id) is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    if not payload.medicines:
        raise HTTPException(status_code=422, detail="A prescription must contain at least one medicine")

    prescription = Prescription(
        patient_id=payload.patient_id,
        doctor_id=doctor.id,
        notes=payload.notes,
        follow_up_date=payload.follow_up_date,
    )
    db.add(prescription)
    await db.flush()

    for line in payload.medicines:
        if await db.get(Medicine, line.medicine_id) is None:
            raise HTTPException(status_code=404, detail=f"Medicine {line.medicine_id} not found")
        db.add(PrescriptionMedicine(prescription_id=prescription.id, **line.model_dump()))

    await db.commit()

    result = await db.execute(
        select(Prescription)
        .options(selectinload(Prescription.medicines))
        .where(Prescription.id == prescription.id)
    )
    saved = result.scalar_one()
    item = PrescriptionResponse.model_validate(saved)
    for med in item.medicines:
        med.medicine_name = med.medicine.name
    return item


@router.get("/prescriptions/patient/{patient_id}", response_model=list[PrescriptionResponse])
async def get_patient_prescriptions(
    patient_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession
):
    await _guard_standing(db, patient_id, doctor, user.id, "prescriptions")
    return await _prescriptions(db, patient_id)


async def _prescriptions(db: DbSession, patient_id: int) -> list[PrescriptionResponse]:
    result = await db.execute(
        select(Prescription)
        .options(selectinload(Prescription.medicines))
        .where(Prescription.patient_id == patient_id)
        .order_by(Prescription.prescription_date.desc())
    )
    payload = []
    for prescription in result.scalars().all():
        item = PrescriptionResponse.model_validate(prescription)
        for med in item.medicines:
            med.medicine_name = med.medicine.name
        payload.append(item)
    return payload


@router.get("/my-patients", response_model=list[PatientResponse])
async def get_my_patients(doctor: CurrentDoctor, db: DbSession):
    """Patients this doctor has recorded/prescribed for, plus anyone who has
    given this doctor (or a hospital they practise at) a live consent."""
    from_records = select(MedicalRecord.patient_id).where(MedicalRecord.doctor_id == doctor.id)
    from_prescriptions = select(Prescription.patient_id).where(Prescription.doctor_id == doctor.id)
    from_consent = select(PatientConsent.patient_id).where(
        PatientConsent.status == ConsentStatus.GRANTED,
        or_(
            PatientConsent.doctor_id == doctor.id,
            PatientConsent.hospital_id == doctor.hospital_id,
        ),
    )

    result = await db.execute(
        select(Patient)
        .where(Patient.id.in_(from_records.union(from_prescriptions, from_consent)))
        .order_by(Patient.first_name, Patient.last_name)
    )
    rows = []
    for patient in result.scalars().all():
        item = PatientResponse.model_validate(patient)
        item.is_family_patient = patient.family_doctor_id == doctor.id
        rows.append(item)
    return rows


# --------------------------------------------------------------------------
# Standing (consent-gated) access to a patient outside a consultation
# --------------------------------------------------------------------------
@router.get("/patients/id/{patient_id}/summary", response_model=PatientResponse)
async def consented_patient_summary(
    patient_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession
):
    """Identity always; allergies / chronic conditions only if shared."""
    patient = await db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")

    # A summary needs *a* live standing consent, not one specific category.
    consent = None
    for category in ("medical_history", "lab_reports", "prescriptions", "uploaded_documents"):
        try:
            consent = await consent_service.require_standing(
                db, patient_id, doctor.id, user.id, category
            )
            break
        except consent_service.ConsentDenied:
            continue
    if consent is None:
        raise HTTPException(status_code=403, detail="This patient has not shared their records with you")

    item = PatientResponse.model_validate(patient)
    if not consent.allow_allergies:
        item.medical_allergies = None
    if not consent.allow_chronic_conditions:
        item.chronic_conditions = None
    if not consent.allow_emergency_contact:
        item.emergency_contact_name = None
        item.emergency_contact_phone = None
    item.is_family_patient = patient.family_doctor_id == doctor.id
    return item


@router.get("/patients/id/{patient_id}/medical-records", response_model=list[MedicalRecordResponse])
async def consented_patient_records(
    patient_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession
):
    await _guard_standing(db, patient_id, doctor, user.id, "medical_history")
    result = await db.execute(
        select(MedicalRecord)
        .where(MedicalRecord.patient_id == patient_id)
        .order_by(MedicalRecord.visit_date.desc())
    )
    return [MedicalRecordResponse.model_validate(r) for r in result.scalars().all()]


@router.get("/patients/id/{patient_id}/prescriptions", response_model=list[PrescriptionResponse])
async def consented_patient_prescriptions(
    patient_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession
):
    await _guard_standing(db, patient_id, doctor, user.id, "prescriptions")
    return await _prescriptions(db, patient_id)


@router.get("/patients/id/{patient_id}/lab-reports", response_model=list[LabReportResponse])
async def consented_patient_lab_reports(
    patient_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession
):
    await _guard_standing(db, patient_id, doctor, user.id, "lab_reports")
    result = await db.execute(
        select(LabReport)
        .where(LabReport.patient_id == patient_id)
        .order_by(LabReport.requested_date.desc())
    )
    return [LabReportResponse.model_validate(r) for r in result.scalars().all()]


@router.get("/patients/id/{patient_id}/documents", response_model=list[MedicalDocumentResponse])
async def consented_patient_documents(
    patient_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession
):
    consent = await _guard_standing(db, patient_id, doctor, user.id, "uploaded_documents")
    result = await db.execute(
        select(MedicalDocument)
        .where(MedicalDocument.patient_id == patient_id)
        .order_by(MedicalDocument.created_at.desc())
    )
    documents = consent_service.filter_documents(consent, list(result.scalars().all()))
    return [
        MedicalDocumentResponse(
            id=d.id,
            file_name=d.file_name,
            document_type=d.document_type.value,
            description=d.description,
            document_date=d.document_date,
            doctor_name=d.doctor_name,
            hospital_name=d.hospital_name,
            status=d.status.value,
            file_size=d.file_size,
            created_at=d.created_at,
        )
        for d in documents
    ]


@router.get("/patients/id/{patient_id}/documents/{document_id}/download")
async def download_consented_document(
    patient_id: int, document_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession
):
    consent = await _guard_standing(db, patient_id, doctor, user.id, "uploaded_documents")
    document = await db.get(MedicalDocument, document_id)
    if document is None or document.patient_id != patient_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if consent_service.filter_documents(consent, [document]) == []:
        raise HTTPException(status_code=403, detail="This document was not shared with you")

    try:
        url = storage.presigned_url(document.storage_key, document.file_name)
    except storage.StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)
