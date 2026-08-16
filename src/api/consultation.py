"""The doctor's view of a patient during a consultation.

Every read here is gated on the patient's consent. The frontend only requests
the tabs it believes are permitted; this router assumes the client is lying and
re-checks each one. Denials are 403 with a patient-facing reason, which the UI
already catches and renders as an empty tab.

Response shapes are envelopes ({medical_records: [...]}) except the patient
endpoint, which the page reads bare.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from src.core.deps import CurrentDoctor, CurrentUser, DbSession
from src.models.clinical import (
    LabReport,
    MedicalRecord,
    Medicine,
    Prescription,
    PrescriptionMedicine,
)
from src.models.consent import MedicalDocument
from src.models.people import Patient
from src.models.queue import QueueEntry
from src.models.user import User
from src.schemas.consent import (
    ConsentResponse,
    ConsentsEnvelope,
    ConsultationLabReport,
    ConsultationMedicalRecord,
    ConsultationPatientInfo,
    ConsultationPrescription,
    ConsultationPrescriptionCreate,
    ConsultationPrescriptionMedicine,
    DocumentsEnvelope,
    LabReportsEnvelope,
    MedicalDocumentResponse,
    MedicalHistoryEnvelope,
    MedicineSearchResult,
    PrescriptionsEnvelope,
)
from src.services import consent as consent_service

router = APIRouter(prefix="/api/consultation", tags=["Consultation"])


async def _entry_for_doctor(db: DbSession, queue_id: int, doctor_id: int) -> QueueEntry:
    entry = await db.get(QueueEntry, queue_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Consultation not found")
    if entry.doctor_id != doctor_id:
        raise HTTPException(status_code=403, detail="This patient is not in your queue")
    return entry


async def _guard(db: DbSession, entry: QueueEntry, doctor_id: int, user_id: int, category: str):
    try:
        return await consent_service.require(db, entry, doctor_id, user_id, category)
    except consent_service.ConsentDenied as exc:
        raise HTTPException(status_code=403, detail=exc.message) from exc


@router.get("/patient/{queue_id}", response_model=ConsultationPatientInfo)
async def consultation_patient(queue_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession):
    """Identity and demographics, plus the sensitive fields the patient chose
    to share. Returned bare -- the page reads this object directly."""
    entry = await _entry_for_doctor(db, queue_id, doctor.id)
    patient = await db.get(Patient, entry.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")

    info = ConsultationPatientInfo(
        id=patient.id,
        uhid=patient.uhid,
        first_name=patient.first_name,
        last_name=patient.last_name,
        date_of_birth=patient.date_of_birth,
        gender=patient.gender.value,
        blood_group=patient.blood_group,
        phone=patient.phone,
    )

    # Identity is needed to treat someone at all; allergies, conditions and
    # next-of-kin are separately consented and stay absent otherwise.
    consent = await consent_service.resolve_for_consultation(db, entry, doctor.id)
    if consent is not None:
        if consent.allow_allergies:
            info.medical_allergies = patient.medical_allergies
        if consent.allow_chronic_conditions:
            info.chronic_conditions = patient.chronic_conditions
        if consent.allow_emergency_contact:
            info.emergency_contact_name = patient.emergency_contact_name
            info.emergency_contact_phone = patient.emergency_contact_phone

    return info


@router.get("/{queue_id}/consent", response_model=ConsentsEnvelope)
async def consultation_consent(queue_id: int, doctor: CurrentDoctor, db: DbSession):
    """What the patient has shared. The chart uses this to decide which tabs to
    show; the server still re-checks each one."""
    entry = await _entry_for_doctor(db, queue_id, doctor.id)
    consent = await consent_service.resolve_for_consultation(db, entry, doctor.id)
    if consent is None:
        return ConsentsEnvelope(consents=[])

    from src.api.documents import _consent_to_response

    return ConsentsEnvelope(consents=[await _consent_to_response(db, consent)])


@router.get("/{queue_id}/medical-history", response_model=MedicalHistoryEnvelope)
async def consultation_medical_history(
    queue_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession
):
    entry = await _entry_for_doctor(db, queue_id, doctor.id)
    await _guard(db, entry, doctor.id, user.id, "medical_history")

    result = await db.execute(
        select(MedicalRecord)
        .where(MedicalRecord.patient_id == entry.patient_id)
        .order_by(MedicalRecord.visit_date.desc())
    )
    records = list(result.scalars().all())

    names: dict[int, str] = {}
    if records:
        from src.models.people import Doctor as DoctorModel

        rows = await db.execute(
            select(DoctorModel.id, User.full_name)
            .join(User, DoctorModel.user_id == User.id)
            .where(DoctorModel.id.in_({r.doctor_id for r in records}))
        )
        names = {row[0]: row[1] or "Unknown" for row in rows.all()}

    return MedicalHistoryEnvelope(
        medical_records=[
            ConsultationMedicalRecord(
                id=r.id,
                chief_complaint=r.chief_complaint,
                diagnosis=r.diagnosis,
                treatment_plan=r.treatment_plan,
                notes=r.notes,
                vitals=r.vitals,
                visit_date=r.visit_date,
                doctor_name=names.get(r.doctor_id),
            )
            for r in records
        ]
    )


@router.get("/{queue_id}/prescriptions", response_model=PrescriptionsEnvelope)
async def consultation_prescriptions(queue_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession):
    entry = await _entry_for_doctor(db, queue_id, doctor.id)
    await _guard(db, entry, doctor.id, user.id, "prescriptions")

    result = await db.execute(
        select(Prescription)
        .options(selectinload(Prescription.medicines))
        .where(Prescription.patient_id == entry.patient_id)
        .order_by(Prescription.prescription_date.desc())
    )
    prescriptions = list(result.scalars().all())

    return PrescriptionsEnvelope(
        prescriptions=[
            ConsultationPrescription(
                id=p.id,
                prescription_date=p.prescription_date,
                status=p.status.value,
                notes=p.notes,
                follow_up_date=p.follow_up_date,
                medicines=[
                    ConsultationPrescriptionMedicine(
                        medicine_name=m.medicine.name if m.medicine else "Unknown",
                        dosage=m.dosage,
                        frequency=m.frequency,
                        duration=m.duration,
                        instructions=m.instructions,
                        quantity_prescribed=m.quantity_prescribed,
                    )
                    for m in p.medicines
                ],
            )
            for p in prescriptions
        ]
    )


@router.get("/{queue_id}/lab-reports", response_model=LabReportsEnvelope)
async def consultation_lab_reports(queue_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession):
    entry = await _entry_for_doctor(db, queue_id, doctor.id)
    await _guard(db, entry, doctor.id, user.id, "lab_reports")

    result = await db.execute(
        select(LabReport)
        .where(LabReport.patient_id == entry.patient_id)
        .order_by(LabReport.requested_date.desc())
    )
    return LabReportsEnvelope(
        lab_reports=[
            ConsultationLabReport(
                id=r.id,
                test_name=r.test_name,
                test_type=r.test_type,
                results=r.results,
                reference_ranges=r.reference_ranges,
                interpretation=r.interpretation,
                status=r.status.value,
                requested_date=r.requested_date,
                completed_date=r.completed_date,
            )
            for r in result.scalars().all()
        ]
    )


@router.get("/{queue_id}/documents", response_model=DocumentsEnvelope)
async def consultation_documents(queue_id: int, doctor: CurrentDoctor, user: CurrentUser, db: DbSession):
    entry = await _entry_for_doctor(db, queue_id, doctor.id)
    consent = await _guard(db, entry, doctor.id, user.id, "uploaded_documents")

    result = await db.execute(
        select(MedicalDocument)
        .where(MedicalDocument.patient_id == entry.patient_id)
        .order_by(MedicalDocument.created_at.desc())
    )
    # Type allow-list and per-document exclusions narrow the category flag.
    documents = consent_service.filter_documents(consent, list(result.scalars().all()))

    return DocumentsEnvelope(
        documents=[
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
    )


@router.get("/medicines/search", response_model=list[MedicineSearchResult])
async def search_medicines(
    doctor: CurrentDoctor,
    db: DbSession,
    query: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Autocomplete for the Rx pad. Defined in the client but never wired up."""
    pattern = f"%{query.lower()}%"
    result = await db.execute(
        select(Medicine)
        .where(
            Medicine.is_active.is_(True),
            or_(
                func.lower(Medicine.name).like(pattern),
                func.lower(Medicine.generic_name).like(pattern),
            ),
        )
        .order_by(Medicine.name)
        .limit(limit)
    )
    return [
        MedicineSearchResult(
            id=m.id,
            name=m.name,
            generic_name=m.generic_name,
            strength=m.strength,
            form=m.form,
            manufacturer=m.manufacturer,
        )
        for m in result.scalars().all()
    ]


async def _resolve_medicine(db: DbSession, name: str) -> Medicine:
    """Find a catalogue entry by name, or create one.

    The Rx pad sends free text, not an id. Refusing an unknown medicine would
    block a doctor mid-consultation over a spelling difference, so unknown names
    are added rather than rejected.
    """
    cleaned = name.strip()
    result = await db.execute(select(Medicine).where(func.lower(Medicine.name) == cleaned.lower()).limit(1))
    medicine = result.scalar_one_or_none()
    if medicine is not None:
        return medicine

    medicine = Medicine(
        name=cleaned[:255],
        manufacturer="Unknown",
        strength="As prescribed",
        form="as directed",
        is_active=True,
    )
    db.add(medicine)
    await db.flush()
    return medicine


@router.post("/{queue_id}/prescription", status_code=status.HTTP_201_CREATED)
async def create_consultation_prescription(
    queue_id: int,
    payload: ConsultationPrescriptionCreate,
    doctor: CurrentDoctor,
    db: DbSession,
):
    """Write a prescription from the consultation.

    Writing is not a consent-gated read -- a doctor treating a patient may
    always prescribe -- but the consultation must be live and theirs.
    """
    entry = await _entry_for_doctor(db, queue_id, doctor.id)
    from src.models.enums import QueueStatus

    if entry.status not in (QueueStatus.WAITING, QueueStatus.IN_CONSULTATION):
        raise HTTPException(
            status_code=409, detail="This consultation has ended and cannot be prescribed against"
        )

    prescription = Prescription(
        patient_id=entry.patient_id,
        doctor_id=doctor.id,
        notes=payload.notes,
        follow_up_date=payload.follow_up_date,
        prescription_date=datetime.now(UTC),
    )
    db.add(prescription)
    await db.flush()

    for line in payload.medicines:
        medicine = await _resolve_medicine(db, line.medicine_name)
        db.add(
            PrescriptionMedicine(
                prescription_id=prescription.id,
                medicine_id=medicine.id,
                dosage=line.dosage,
                frequency=line.frequency,
                duration=line.duration,
                instructions=line.instructions,
                quantity_prescribed=line.quantity_prescribed,
            )
        )

    await db.commit()
    await db.refresh(prescription)
    return {
        "success": True,
        "prescription_id": prescription.id,
        "patient_id": entry.patient_id,
        "medicine_count": len(payload.medicines),
    }


__all__ = ["ConsentResponse", "router"]
