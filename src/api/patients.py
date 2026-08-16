from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.core.deps import CurrentPatient, CurrentUser, DbSession
from src.models.clinical import LabReport, MedicalRecord, Prescription
from src.models.enums import UserRole
from src.models.people import Doctor, Patient
from src.models.user import User
from src.schemas.healthcare import (
    LabReportResponse,
    MedicalRecordResponse,
    PatientResponse,
    PrescriptionResponse,
    QRAccessRequest,
    QRAccessResponse,
)
from src.services.qr import create_qr_grant, resolve_grant

router = APIRouter(prefix="/api/patients", tags=["Patients"])


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
async def get_my_profile(patient: CurrentPatient):
    return PatientResponse.model_validate(patient)


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
