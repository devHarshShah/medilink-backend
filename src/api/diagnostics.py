"""Diagnostic centre portal and lab ordering.

The loop: a doctor orders a test during a consultation, the patient shows their
QR at the lab, the lab publishes the result, and it becomes visible to the
patient and the ordering doctor with no paper in between.
"""

from datetime import UTC, date, datetime

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from src.core.deps import CurrentDiagnosticCenter, CurrentDoctor, DbSession
from src.models.clinical import LabReport
from src.models.enums import QueueStatus, TestStatus
from src.models.organizations import DiagnosticCenter
from src.models.people import Doctor, Patient
from src.models.queue import QueueEntry
from src.models.user import User
from src.schemas.diagnostics import (
    DiagnosticCenterStats,
    LabReportDetail,
    LabRequestCreate,
    LabRequestFromConsultation,
    LabResultUpdate,
    LabStatusUpdate,
)
from src.schemas.healthcare import PatientResponse
from src.services import storage
from src.services.qr import resolve_grant

router = APIRouter(prefix="/api/diagnostics", tags=["Diagnostics"])
doctor_router = APIRouter(prefix="/api/doctor", tags=["Doctor"])


async def _to_detail(db: DbSession, report: LabReport) -> LabReportDetail:
    patient = await db.get(Patient, report.patient_id)
    doctor_name = None
    if report.requested_by_doctor_id:
        doctor = await db.get(Doctor, report.requested_by_doctor_id)
        if doctor:
            user = await db.get(User, doctor.user_id)
            doctor_name = user.full_name if user else None

    return LabReportDetail(
        id=report.id,
        patient_id=report.patient_id,
        patient_uhid=patient.uhid if patient else None,
        patient_name=patient.full_name if patient else None,
        diagnostic_center_id=report.diagnostic_center_id,
        requested_by_doctor_id=report.requested_by_doctor_id,
        requested_by_doctor_name=doctor_name,
        test_name=report.test_name,
        test_type=report.test_type,
        results=report.results,
        reference_ranges=report.reference_ranges,
        interpretation=report.interpretation,
        technician_notes=report.technician_notes,
        status=report.status,
        requested_date=report.requested_date,
        completed_date=report.completed_date,
        has_attachment=bool(report.report_file_path),
    )


# --------------------------------------------------------------------------
# Ordering (doctor side)
# --------------------------------------------------------------------------
@doctor_router.post("/lab-requests", response_model=LabReportDetail, status_code=201)
async def create_lab_request(payload: LabRequestCreate, doctor: CurrentDoctor, db: DbSession):
    """Order a test for a patient."""
    if await db.get(Patient, payload.patient_id) is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    centre = await db.get(DiagnosticCenter, payload.diagnostic_center_id)
    if centre is None or not centre.is_active:
        raise HTTPException(status_code=404, detail="Diagnostic centre not found")

    report = LabReport(
        patient_id=payload.patient_id,
        diagnostic_center_id=payload.diagnostic_center_id,
        requested_by_doctor_id=doctor.id,
        test_name=payload.test_name,
        test_type=payload.test_type,
        technician_notes=payload.notes,
        status=TestStatus.REQUESTED,
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return await _to_detail(db, report)


@doctor_router.post("/consultation/{queue_id}/lab-request", response_model=LabReportDetail, status_code=201)
async def create_lab_request_from_consultation(
    queue_id: int,
    payload: LabRequestFromConsultation,
    doctor: CurrentDoctor,
    db: DbSession,
):
    """Order a test from inside a live consultation."""
    entry = await db.get(QueueEntry, queue_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Consultation not found")
    if entry.doctor_id != doctor.id:
        raise HTTPException(status_code=403, detail="This patient is not in your queue")
    if entry.status not in (QueueStatus.WAITING, QueueStatus.IN_CONSULTATION):
        raise HTTPException(status_code=409, detail="This consultation has ended")

    centre = await db.get(DiagnosticCenter, payload.diagnostic_center_id)
    if centre is None or not centre.is_active:
        raise HTTPException(status_code=404, detail="Diagnostic centre not found")

    report = LabReport(
        patient_id=entry.patient_id,
        diagnostic_center_id=payload.diagnostic_center_id,
        requested_by_doctor_id=doctor.id,
        test_name=payload.test_name,
        test_type=payload.test_type,
        technician_notes=payload.notes,
        status=TestStatus.REQUESTED,
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return await _to_detail(db, report)


@doctor_router.get("/lab-requests", response_model=list[LabReportDetail])
async def my_lab_requests(doctor: CurrentDoctor, db: DbSession):
    """Tests this doctor ordered, newest first. Results land here when the lab
    publishes them -- no chasing paper."""
    result = await db.execute(
        select(LabReport)
        .where(LabReport.requested_by_doctor_id == doctor.id)
        .order_by(LabReport.requested_date.desc())
    )
    return [await _to_detail(db, r) for r in result.scalars().all()]


# --------------------------------------------------------------------------
# Diagnostic centre portal
# --------------------------------------------------------------------------
@router.get("/requests", response_model=list[LabReportDetail])
async def centre_requests(
    centre: CurrentDiagnosticCenter,
    db: DbSession,
    status_filter: TestStatus | None = None,
):
    stmt = select(LabReport).where(LabReport.diagnostic_center_id == centre.id)
    if status_filter is not None:
        stmt = stmt.where(LabReport.status == status_filter)
    result = await db.execute(stmt.order_by(LabReport.requested_date.desc()))
    return [await _to_detail(db, r) for r in result.scalars().all()]


@router.get("/patient/qr/{access_token}", response_model=PatientResponse)
async def patient_by_qr(access_token: str, centre: CurrentDiagnosticCenter, db: DbSession):
    """Identify a walk-in patient from the QR on their phone."""
    try:
        _, patient = await resolve_grant(db, access_token, required_scope="profile")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return PatientResponse.model_validate(patient)


@router.get("/requests/qr/{access_token}", response_model=list[LabReportDetail])
async def requests_by_qr(access_token: str, centre: CurrentDiagnosticCenter, db: DbSession):
    """The tests waiting for the patient standing at the counter."""
    try:
        _, patient = await resolve_grant(db, access_token, required_scope="lab_reports")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    result = await db.execute(
        select(LabReport)
        .where(
            LabReport.patient_id == patient.id,
            LabReport.diagnostic_center_id == centre.id,
            LabReport.status.in_([TestStatus.REQUESTED, TestStatus.IN_PROGRESS]),
        )
        .order_by(LabReport.requested_date.desc())
    )
    return [await _to_detail(db, r) for r in result.scalars().all()]


# Which transitions a centre may make. A completed report cannot silently
# reopen -- the doctor may already have acted on the result.
ALLOWED_TRANSITIONS: dict[TestStatus, set[TestStatus]] = {
    TestStatus.REQUESTED: {TestStatus.IN_PROGRESS, TestStatus.CANCELLED},
    TestStatus.IN_PROGRESS: {TestStatus.COMPLETED, TestStatus.CANCELLED},
    TestStatus.COMPLETED: set(),
    TestStatus.CANCELLED: set(),
}


async def _owned_report(db: DbSession, report_id: int, centre_id: int) -> LabReport:
    report = await db.get(LabReport, report_id)
    if report is None or report.diagnostic_center_id != centre_id:
        raise HTTPException(status_code=404, detail="Test request not found")
    return report


@router.patch("/requests/{report_id}/status", response_model=LabReportDetail)
async def update_request_status(
    report_id: int,
    payload: LabStatusUpdate,
    centre: CurrentDiagnosticCenter,
    db: DbSession,
):
    report = await _owned_report(db, report_id, centre.id)
    if payload.new_status not in ALLOWED_TRANSITIONS[report.status]:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot move a {report.status.value} test to {payload.new_status.value}",
        )

    report.status = payload.new_status
    if payload.new_status is TestStatus.COMPLETED:
        report.completed_date = datetime.now(UTC)
    await db.commit()
    await db.refresh(report)
    return await _to_detail(db, report)


@router.put("/requests/{report_id}/results", response_model=LabReportDetail)
async def publish_results(
    report_id: int,
    payload: LabResultUpdate,
    centre: CurrentDiagnosticCenter,
    db: DbSession,
):
    """Publish the result. Visible to the patient and the ordering doctor at once."""
    report = await _owned_report(db, report_id, centre.id)
    if report.status is TestStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="This test was cancelled")
    if report.status is TestStatus.COMPLETED:
        raise HTTPException(
            status_code=409,
            detail="Results have already been published. Raise a fresh test to correct them.",
        )

    if payload.results is not None:
        report.results = payload.results
    if payload.reference_ranges is not None:
        report.reference_ranges = payload.reference_ranges
    if payload.interpretation is not None:
        report.interpretation = payload.interpretation
    if payload.technician_notes is not None:
        report.technician_notes = payload.technician_notes

    # A completed test with no findings is indistinguishable from one nobody
    # filled in, so require something before closing it.
    if not report.results and not report.report_file_path:
        raise HTTPException(
            status_code=422, detail="Add results or attach a report before completing the test"
        )

    report.status = TestStatus.COMPLETED
    report.completed_date = datetime.now(UTC)
    await db.commit()
    await db.refresh(report)
    return await _to_detail(db, report)


@router.post("/requests/{report_id}/attachment", response_model=LabReportDetail)
async def attach_report_file(
    report_id: int,
    centre: CurrentDiagnosticCenter,
    db: DbSession,
    file: UploadFile = File(...),
):
    report = await _owned_report(db, report_id, centre.id)
    if report.status is TestStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="This test was cancelled")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="The file is empty")

    from src.core.config import settings

    if len(data) > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"Files must be {limit_mb}MB or smaller")

    sniffed = storage.sniff_content_type(data[:16])
    if sniffed is None or sniffed not in settings.allowed_upload_types:
        raise HTTPException(status_code=422, detail="Only JPEG, PNG and PDF files can be uploaded")

    key = storage.build_key(report.patient_id, file.filename or "report")
    try:
        storage.upload(key, data, sniffed)
    except storage.StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    report.report_file_path = key
    await db.commit()
    await db.refresh(report)
    return await _to_detail(db, report)


@router.get("/requests/{report_id}/attachment")
async def download_attachment(report_id: int, centre: CurrentDiagnosticCenter, db: DbSession):
    report = await _owned_report(db, report_id, centre.id)
    if not report.report_file_path:
        raise HTTPException(status_code=404, detail="No report file is attached")
    try:
        url = storage.presigned_url(report.report_file_path, f"{report.test_name}.pdf")
    except storage.StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get("/stats", response_model=DiagnosticCenterStats)
async def centre_stats(centre: CurrentDiagnosticCenter, db: DbSession):
    async def count(*conditions) -> int:
        result = await db.execute(
            select(func.count(LabReport.id)).where(LabReport.diagnostic_center_id == centre.id, *conditions)
        )
        return int(result.scalar_one())

    return DiagnosticCenterStats(
        pending_requests=await count(LabReport.status == TestStatus.REQUESTED),
        in_progress=await count(LabReport.status == TestStatus.IN_PROGRESS),
        completed_today=await count(
            LabReport.status == TestStatus.COMPLETED,
            func.date(LabReport.completed_date) == date.today(),
        ),
        total_completed=await count(LabReport.status == TestStatus.COMPLETED),
    )
