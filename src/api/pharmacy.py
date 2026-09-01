from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.core.deps import CurrentPharmacy, DbSession
from src.models.clinical import Prescription
from src.models.enums import PrescriptionStatus
from src.models.people import Doctor, Patient
from src.models.user import User
from src.schemas.healthcare import PatientResponse, PrescriptionResponse
from src.services.qr import resolve_grant

router = APIRouter(prefix="/api/pharmacy", tags=["Pharmacy"])


async def _enrich(db: DbSession, items: list[PrescriptionResponse]) -> list[PrescriptionResponse]:
    """Flatten patient and doctor names onto the rows - the pharmacy UI renders
    `patient_name` / `patient_uhid` / `doctor_name` directly."""
    if not items:
        return items

    doc_names = {
        row[0]: row[1]
        for row in (
            await db.execute(
                select(Doctor.id, User.full_name)
                .join(User, Doctor.user_id == User.id)
                .where(Doctor.id.in_({i.doctor_id for i in items}))
            )
        ).all()
    }
    patients = {
        p.id: p
        for p in (
            await db.execute(select(Patient).where(Patient.id.in_({i.patient_id for i in items})))
        )
        .scalars()
        .all()
    }

    for item in items:
        item.doctor_name = doc_names.get(item.doctor_id)
        patient = patients.get(item.patient_id)
        if patient is not None:
            item.patient_name = f"{patient.first_name} {patient.last_name}"
            item.patient_uhid = patient.uhid
    return items


async def _prescriptions_for_patient(db: DbSession, patient_id: int) -> list[PrescriptionResponse]:
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
    return await _enrich(db, payload)


@router.get("/prescriptions/uhid/{uhid}", response_model=list[PrescriptionResponse])
async def get_prescriptions_by_uhid(uhid: str, pharmacy: CurrentPharmacy, db: DbSession):
    result = await db.execute(select(Patient).where(Patient.uhid == uhid.upper()))
    patient = result.scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="No patient found with that UHID")
    return await _prescriptions_for_patient(db, patient.id)


@router.get("/prescriptions/qr/{access_token}", response_model=list[PrescriptionResponse])
async def get_prescriptions_by_qr(access_token: str, pharmacy: CurrentPharmacy, db: DbSession):
    try:
        _, patient = await resolve_grant(db, access_token, required_scope="prescriptions")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return await _prescriptions_for_patient(db, patient.id)


@router.get("/patient-info/qr/{access_token}", response_model=PatientResponse)
async def get_patient_info_by_qr(access_token: str, pharmacy: CurrentPharmacy, db: DbSession):
    try:
        _, patient = await resolve_grant(db, access_token, required_scope="profile")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return PatientResponse.model_validate(patient)


@router.post("/dispense/{prescription_id}", response_model=PrescriptionResponse)
async def dispense_prescription(prescription_id: int, pharmacy: CurrentPharmacy, db: DbSession):
    """Hand over the medicine and burn the prescription so it cannot be reused."""
    result = await db.execute(
        select(Prescription)
        .options(selectinload(Prescription.medicines))
        .where(Prescription.id == prescription_id)
    )
    prescription = result.scalar_one_or_none()
    if prescription is None:
        raise HTTPException(status_code=404, detail="Prescription not found")

    if prescription.status == PrescriptionStatus.DISPENSED:
        raise HTTPException(status_code=409, detail="This prescription has already been dispensed")
    if prescription.status != PrescriptionStatus.ACTIVE:
        raise HTTPException(
            status_code=409,
            detail=f"This prescription is {prescription.status.value} and cannot be dispensed",
        )

    prescription.status = PrescriptionStatus.DISPENSED
    prescription.dispensed_at = datetime.now(UTC)
    prescription.dispensed_by_pharmacy_id = pharmacy.id
    await db.commit()
    await db.refresh(prescription)

    item = PrescriptionResponse.model_validate(prescription)
    for med in item.medicines:
        med.medicine_name = med.medicine.name
    (item,) = await _enrich(db, [item])
    return item


@router.get("/dispensed-medicines", response_model=list[PrescriptionResponse])
async def get_dispensed_medicines(pharmacy: CurrentPharmacy, db: DbSession):
    result = await db.execute(
        select(Prescription)
        .options(selectinload(Prescription.medicines))
        .where(Prescription.dispensed_by_pharmacy_id == pharmacy.id)
        .order_by(Prescription.dispensed_at.desc())
    )
    payload = []
    for prescription in result.scalars().all():
        item = PrescriptionResponse.model_validate(prescription)
        for med in item.medicines:
            med.medicine_name = med.medicine.name
        payload.append(item)
    return await _enrich(db, payload)
