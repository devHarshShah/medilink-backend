from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.core.deps import CurrentPatient, CurrentUser, DbSession
from src.models.clinical import LabReport, MedicalRecord, Prescription
from src.models.consent import PatientConsent
from src.models.enums import ConsentStatus, ConsentType, UserRole
from src.models.people import Doctor, Patient
from src.models.user import User
from src.schemas.healthcare import (
    FamilyDoctorRequest,
    LabReportResponse,
    MedicalRecordResponse,
    PatientResponse,
    PatientUpdate,
    PrescriptionResponse,
    QRAccessRequest,
    QRAccessResponse,
)
from src.services.qr import create_qr_grant, resolve_grant

router = APIRouter(prefix="/api/patients", tags=["Patients"])


async def _profile_response(db: DbSession, patient: Patient) -> PatientResponse:
    item = PatientResponse.model_validate(patient)
    if patient.family_doctor_id is not None:
        names = await _doctor_names(db, {patient.family_doctor_id})
        item.family_doctor_name = names.get(patient.family_doctor_id)
    return item


async def _doctor_names(db: DbSession, doctor_ids: set[int]) -> dict[int, str]:
    """Flatten doctor names onto records - the UI renders `doctor_name` directly."""
    if not doctor_ids:
        return {}
    result = await db.execute(
        select(Doctor.id, User.full_name)
        .join(User, Doctor.user_id == User.id)
        .where(Doctor.id.in_(doctor_ids))
    )
    return {row[0]: row[1] or "Unknown" for row in result.all()}


@router.get("/me", response_model=PatientResponse)
async def get_my_profile(patient: CurrentPatient, db: DbSession):
    return await _profile_response(db, patient)


@router.patch("/me", response_model=PatientResponse)
async def update_my_profile(payload: PatientUpdate, patient: CurrentPatient, db: DbSession):
    """Update the signed-in patient's own profile. Only the fields sent are touched."""
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(patient, field, value)
    await db.commit()
    await db.refresh(patient)
    return await _profile_response(db, patient)


@router.put("/me/family-doctor", response_model=PatientResponse)
async def set_family_doctor(payload: FamilyDoctorRequest, patient: CurrentPatient, db: DbSession):
    """Choose a family doctor and grant them standing access to the chosen
    categories, via an ongoing-care consent scoped to that doctor."""
    doctor = await db.get(Doctor, payload.doctor_id)
    if doctor is None:
        raise HTTPException(status_code=404, detail="Doctor not found")

    # Revoke any previous family-doctor grant (a different doctor, or stale flags).
    existing = (
        await db.execute(
            select(PatientConsent).where(
                PatientConsent.patient_id == patient.id,
                PatientConsent.consent_type == ConsentType.ONGOING_CARE,
                PatientConsent.status == ConsentStatus.GRANTED,
            )
        )
    ).scalars().all()
    for consent in existing:
        consent.status = ConsentStatus.REVOKED
        consent.revoked_at = datetime.now(UTC)

    db.add(
        PatientConsent(
            patient_id=patient.id,
            doctor_id=doctor.id,
            consent_type=ConsentType.ONGOING_CARE,
            status=ConsentStatus.GRANTED,
            purpose="Family doctor",
            allow_medical_history=payload.allow_medical_history,
            allow_lab_reports=payload.allow_lab_reports,
            allow_prescriptions=payload.allow_prescriptions,
            allow_uploaded_documents=payload.allow_uploaded_documents,
        )
    )
    patient.family_doctor_id = doctor.id
    await db.commit()
    await db.refresh(patient)
    return await _profile_response(db, patient)


@router.delete("/me/family-doctor", response_model=PatientResponse)
async def clear_family_doctor(patient: CurrentPatient, db: DbSession):
    """Remove the family doctor and revoke the standing consent behind it."""
    existing = (
        await db.execute(
            select(PatientConsent).where(
                PatientConsent.patient_id == patient.id,
                PatientConsent.consent_type == ConsentType.ONGOING_CARE,
                PatientConsent.status == ConsentStatus.GRANTED,
            )
        )
    ).scalars().all()
    for consent in existing:
        consent.status = ConsentStatus.REVOKED
        consent.revoked_at = datetime.now(UTC)

    patient.family_doctor_id = None
    await db.commit()
    await db.refresh(patient)
    return await _profile_response(db, patient)


@router.post("/me/qr-access", response_model=QRAccessResponse)
async def generate_qr_access(payload: QRAccessRequest, patient: CurrentPatient, db: DbSession):
    """Generate QR code for temporary access to patient data."""
    grant, qr_code = await create_qr_grant(db, patient, payload.access_type, payload.ttl_minutes)
    return QRAccessResponse(
        qr_code=qr_code,
        access_token=grant.access_token,
        expires_at=grant.expires_at,
        access_type=grant.access_type,
    )


@router.get("/me/medical-records", response_model=list[MedicalRecordResponse])
async def get_my_medical_records(patient: CurrentPatient, db: DbSession):
    result = await db.execute(
        select(MedicalRecord)
        .where(MedicalRecord.patient_id == patient.id)
        .order_by(MedicalRecord.visit_date.desc())
    )
    records = list(result.scalars().all())
    names = await _doctor_names(db, {r.doctor_id for r in records})

    payload = []
    for record in records:
        item = MedicalRecordResponse.model_validate(record)
        item.doctor_name = names.get(record.doctor_id)
        payload.append(item)
    return payload


@router.get("/me/prescriptions", response_model=list[PrescriptionResponse])
async def get_my_prescriptions(patient: CurrentPatient, db: DbSession):
    result = await db.execute(
        select(Prescription)
        .options(selectinload(Prescription.medicines))
        .where(Prescription.patient_id == patient.id)
        .order_by(Prescription.prescription_date.desc())
    )
    prescriptions = list(result.scalars().all())
    names = await _doctor_names(db, {p.doctor_id for p in prescriptions})

    payload = []
    for prescription in prescriptions:
        item = PrescriptionResponse.model_validate(prescription)
        item.doctor_name = names.get(prescription.doctor_id)
        for med in item.medicines:
            med.medicine_name = med.medicine.name
        payload.append(item)
    return payload


@router.get("/me/lab-reports", response_model=list[LabReportResponse])
async def get_my_lab_reports(patient: CurrentPatient, db: DbSession):
    result = await db.execute(
        select(LabReport).where(LabReport.patient_id == patient.id).order_by(LabReport.requested_date.desc())
    )
    return [LabReportResponse.model_validate(r) for r in result.scalars().all()]


@router.get("/{uhid}", response_model=PatientResponse)
async def get_patient_by_uhid(uhid: str, user: CurrentUser, db: DbSession):
    """Look a patient up by UHID. Restricted to clinical and dispensing roles -
    a patient may only fetch themselves this way."""
    result = await db.execute(select(Patient).where(Patient.uhid == uhid.upper()))
    patient = result.scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="No patient found with that UHID")

    allowed = {
        UserRole.ADMIN,
        UserRole.DOCTOR,
        UserRole.NURSE,
        UserRole.PHARMACY,
        UserRole.DIAGNOSTIC_CENTER,
    }
    if user.role not in allowed and patient.user_id != user.id:
        raise HTTPException(status_code=403, detail="You do not have access to this patient's record")

    return PatientResponse.model_validate(patient)


@router.post("/qr-access/{access_token}", response_model=PatientResponse)
async def access_patient_by_qr(access_token: str, db: DbSession):
    """Redeem a patient-issued QR token for their profile."""
    try:
        _, patient = await resolve_grant(db, access_token, required_scope="profile")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return PatientResponse.model_validate(patient)
