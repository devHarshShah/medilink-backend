"""Diagnostic centre schemas.

Closes the loop: a doctor orders a test, the patient shows a QR at the lab, the
lab publishes results, and they appear for the patient and the ordering doctor
without anyone carrying paper.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from src.models.enums import TestStatus


class LabRequestCreate(BaseModel):
    patient_id: int
    diagnostic_center_id: int
    test_name: str = Field(min_length=1, max_length=255)
    test_type: str = Field(min_length=1, max_length=120)
    notes: str | None = None


class LabRequestFromConsultation(BaseModel):
    diagnostic_center_id: int
    test_name: str = Field(min_length=1, max_length=255)
    test_type: str = Field(min_length=1, max_length=120)
    notes: str | None = None


class LabReportDetail(BaseModel):
    id: int
    patient_id: int
    patient_uhid: str | None = None
    patient_name: str | None = None
    diagnostic_center_id: int
    requested_by_doctor_id: int | None = None
    requested_by_doctor_name: str | None = None
    test_name: str
    test_type: str
    results: str | None = None
    reference_ranges: str | None = None
    interpretation: str | None = None
    technician_notes: str | None = None
    status: TestStatus
    requested_date: datetime
    completed_date: datetime | None = None
    has_attachment: bool = False


class LabResultUpdate(BaseModel):
    results: str | None = None
    reference_ranges: str | None = None
    interpretation: str | None = None
    technician_notes: str | None = None


class LabStatusUpdate(BaseModel):
    new_status: TestStatus


class DiagnosticCenterStats(BaseModel):
    pending_requests: int
    in_progress: int
    completed_today: int
    total_completed: int
