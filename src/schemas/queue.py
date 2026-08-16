"""Queue schemas.

Field-for-field with src/types/api.ts. Anything added beyond that (leave_at,
travel_time_minutes, distance_km) is additive -- the existing frontend ignores
unknown keys, and the new queue UI reads them.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import QueuePriority, QueueStatus, QueueType


class PatientLocation(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class JoinQueueRequest(BaseModel):
    # Plural: the patient offers candidates and the server assigns one.
    doctor_ids: list[int] = Field(min_length=1, max_length=20)
    hospital_id: int | None = None
    symptoms_description: str = Field(min_length=1, max_length=2000)
    ai_recommendation: dict | None = None
    priority: QueuePriority = QueuePriority.NORMAL
    queue_type: QueueType = QueueType.HOSPITAL_SPECIFIC
    patient_location: PatientLocation | None = None


class DoctorQueueInfo(BaseModel):
    doctor_id: int
    doctor_name: str
    specialization: str
    match_score: float | None = None
    queue_size: int
    estimated_wait_time: int
    hospital_id: int | None = None
    hospital_name: str | None = None
    distance: float | None = None


class JoinQueueResponse(BaseModel):
    success: bool
    queue_id: int | None = None
    doctor_id: int | None = None
    doctor_name: str | None = None
    doctor_specialization: str | None = None
    queue_type: QueueType | None = None
    queue_number: int | None = None
    estimated_wait_time: int | None = None
    current_position: int | None = None
    assigned_hospital_id: int | None = None
    assigned_hospital_name: str | None = None
    alternative_options: list[DoctorQueueInfo] = []
    error: str | None = None
    suggested_doctors: list[DoctorQueueInfo] = []
    # Additive: the "wait at home" payload.
    leave_at: datetime | None = None
    travel_time_minutes: int | None = None


class QueueStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    queue_id: int
    patient_id: int
    doctor_id: int
    doctor_name: str
    doctor_specialization: str
    queue_type: QueueType
    hospital_id: int | None = None
    hospital_name: str | None = None
    # The frontend reads both spellings depending on the screen.
    assigned_hospital_id: int | None = None
    assigned_hospital_name: str | None = None
    queue_number: int
    current_position: int
    status: QueueStatus
    priority: QueuePriority
    estimated_wait_time: int | None = None
    joined_at: datetime
    consultation_started_at: datetime | None = None
    consultation_completed_at: datetime | None = None
    patient_notified_at: datetime | None = None
    is_emergency: bool
    ai_summary: str | None = None
    symptoms_description: str | None = None
    # Additive.
    leave_at: datetime | None = None
    travel_time_minutes: int | None = None
    distance_km: float | None = None
    locked_in: bool = False


class MyQueuesResponse(BaseModel):
    """The page reads `response.queues`, not a bare array."""

    queues: list[QueueStatusResponse]


class AvailableDoctorResponse(BaseModel):
    doctor_id: int
    name: str
    specialization: str
    qualification: str
    years_of_experience: int
    consultation_fee: float | None = None
    current_queue_size: int
    estimated_wait_time: int
    availability_status: str
    match_score: float | None = None
    distance: float | None = None
    hospital_id: int | None = None
    hospital_name: str | None = None


class AvailableDoctorsResponse(BaseModel):
    hospital_id: int
    hospital_name: str
    doctors: list[AvailableDoctorResponse]
    total_available: int


class PatientQueueInfo(BaseModel):
    queue_id: int
    patient_uhid: str
    patient_name: str
    queue_number: int
    queue_type: QueueType
    status: QueueStatus
    priority: QueuePriority
    symptoms_description: str | None = None
    estimated_wait_time: int | None = None
    joined_at: datetime
    is_emergency: bool
    hospital_name: str | None = None
    assigned_hospital_name: str | None = None
    ai_summary: str | None = None


class DoctorQueueResponse(BaseModel):
    doctor_id: int
    hospital_id: int | None = None
    queue_type: QueueType | None = None
    queue: list[PatientQueueInfo]
    total_waiting: int
    total_in_consultation: int


class UpdateQueueStatusRequest(BaseModel):
    new_status: QueueStatus
    notes: str | None = None


class StreamTokenResponse(BaseModel):
    """EventSource cannot set headers, so the SSE token rides in the query
    string. Kept short-lived because a query string leaks into logs."""

    token: str
    expires_in: int
