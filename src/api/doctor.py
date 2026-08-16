from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.core.deps import CurrentDoctor, DbSession
from src.models.clinical import MedicalRecord, Medicine, Prescription, PrescriptionMedicine
from src.models.people import Patient
from src.models.user import User
from src.schemas.healthcare import (
    DoctorResponse,
    MedicalRecordCreate,
    MedicalRecordResponse,
    PatientResponse,
    PrescriptionCreate,
    PrescriptionResponse,
)

router = APIRouter(prefix="/api/doctor", tags=["Doctor"])


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
async def get_patient_medical_records(patient_id: int, doctor: CurrentDoctor, db: DbSession):
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
async def get_patient_prescriptions(patient_id: int, doctor: CurrentDoctor, db: DbSession):
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
    """Every patient this doctor has ever recorded or prescribed for."""
    from_records = select(MedicalRecord.patient_id).where(MedicalRecord.doctor_id == doctor.id)
    from_prescriptions = select(Prescription.patient_id).where(Prescription.doctor_id == doctor.id)

    result = await db.execute(
        select(Patient)
        .where(Patient.id.in_(from_records.union(from_prescriptions)))
        .order_by(Patient.first_name, Patient.last_name)
    )
    return [PatientResponse.model_validate(p) for p in result.scalars().all()]
