"""Doctor presence, schedule and stats schemas.

Shapes come from the frontend's ad-hoc interfaces in
components/doctor-checkin.tsx and components/doctor-schedule-manager.tsx.
"""

from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import DoctorAvailabilityStatus, LeaveType


class ScheduleToday(BaseModel):
    start_time: str
    end_time: str
    break_start_time: str | None = None
    break_end_time: str | None = None
    slot_duration: int


class DoctorHospitalResponse(BaseModel):
    """Note the `hospital_`-prefixed keys -- doctor-checkin.tsx reads these,
    not the plain `id`/`name` of HospitalResponse."""

    hospital_id: int
    hospital_name: str
    hospital_address: str
    is_checked_in: bool
    presence_status: str
    current_queue_size: int
    estimated_wait_time: int
    active_checkin_id: int | None = None
    schedule_today: ScheduleToday | None = None
    distance_from_current: float | None = None


class CheckInRequest(BaseModel):
    hospital_id: int


class CheckOutRequest(BaseModel):
    notes: str | None = None
    next_hospital_id: int | None = None


class PresenceStatusRequest(BaseModel):
    # The badge renders "present" but the PUT only ever sends these three;
    # accept the synonym rather than 422 on a value the UI can display.
    status: str = Field(pattern="^(available|busy|on_break|break|present|offline|emergency)$")

    def normalized(self) -> DoctorAvailabilityStatus:
        mapping = {
            "available": DoctorAvailabilityStatus.AVAILABLE,
            "present": DoctorAvailabilityStatus.AVAILABLE,
            "busy": DoctorAvailabilityStatus.BUSY,
            "on_break": DoctorAvailabilityStatus.BREAK,
            "break": DoctorAvailabilityStatus.BREAK,
            "offline": DoctorAvailabilityStatus.OFFLINE,
            "emergency": DoctorAvailabilityStatus.EMERGENCY,
        }
        return mapping[self.status]


class CurrentHospital(BaseModel):
    hospital_id: int
    hospital_name: str
    check_in_time: datetime
    duration_minutes: int


class AvailabilityEntry(BaseModel):
    hospital_id: int
    hospital_name: str
    is_checked_in: bool
    presence_status: str
    current_queue_size: int
    estimated_wait_time: int


class DoctorStatusResponse(BaseModel):
    doctor_id: int
    doctor_name: str
    is_checked_in: bool
    current_hospital: CurrentHospital | None = None
    presence_status: str
    last_activity: datetime | None = None
    availabilities: list[AvailabilityEntry] = []


# --------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class ScheduleCreate(BaseModel):
    hospital_id: int
    day_of_week: int = Field(ge=0, le=6)
    start_time: time
    end_time: time
    break_start_time: time | None = None
    break_end_time: time | None = None
    slot_duration: int = Field(default=15, ge=5, le=120)
    max_patients_per_slot: int = Field(default=1, ge=1, le=20)
    is_available: bool = True
    effective_from: date | None = None
    effective_until: date | None = None
    notes: str | None = None


class ScheduleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    schedule_id: int
    hospital_id: int
    hospital_name: str
    day_of_week: int
    day_name: str
    start_time: str
    end_time: str
    break_start_time: str | None = None
    break_end_time: str | None = None
    slot_duration: int
    max_patients_per_slot: int
    is_available: bool
    effective_from: date | None = None
    effective_until: date | None = None
    notes: str | None = None


class LeaveCreate(BaseModel):
    hospital_id: int | None = None
    leave_type: LeaveType
    start_date: date
    end_date: date
    start_time: time | None = None
    end_time: time | None = None
    reason: str | None = None


class LeaveResponse(BaseModel):
    leave_id: int
    hospital_id: int | None = None
    hospital_name: str | None = None
    leave_type: str
    start_date: date
    end_date: date
    start_time: str | None = None
    end_time: str | None = None
    reason: str | None = None
    is_approved: bool
    approved_by: int | None = None
    created_at: datetime


# --------------------------------------------------------------------------
# Stats (replacing the frontend's hardcoded mock blocks)
# --------------------------------------------------------------------------
class UpcomingScheduleEntry(BaseModel):
    hospital_name: str
    start_time: str
    end_time: str
    patient_count: int


class DoctorStatsResponse(BaseModel):
    today_consultations: int
    current_queue_size: int
    avg_consultation_time: float
    total_wait_time_saved: int
    patient_satisfaction: float | None = None
    upcoming_schedule: list[UpcomingScheduleEntry] = []
    pending_reports: int = 0
    active_prescriptions: int = 0
    total_patients: int = 0


class PharmacyStatsResponse(BaseModel):
    total_prescriptions: int
    today_dispensed: int
    total_revenue: float
