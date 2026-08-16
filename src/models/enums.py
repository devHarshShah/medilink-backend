"""Domain enums.

Values here are the wire format. Several are pinned by the surviving frontend
rather than chosen freely - see the notes on each.
"""

import enum


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    DOCTOR = "doctor"
    NURSE = "nurse"
    PHARMACY = "pharmacy"
    DIAGNOSTIC_CENTER = "diagnostic_center"
    PATIENT = "patient"


class Gender(str, enum.Enum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"


class AccessType(str, enum.Enum):
    FULL_ACCESS = "full_access"
    PRESCRIPTION_ONLY = "prescription_only"
    REPORTS_ONLY = "reports_only"
    EMERGENCY_ACCESS = "emergency_access"


class PrescriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    DISPENSED = "dispensed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class TestStatus(str, enum.Enum):
    # openapi.json is authoritative here; the frontend type said "pending" and is
    # corrected to match (see plan, phase 1 drift reconciliation).
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class QueueStatus(str, enum.Enum):
    WAITING = "waiting"
    IN_CONSULTATION = "in_consultation"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class QueuePriority(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EMERGENCY = "emergency"

    @property
    def weight(self) -> int:
        """Higher wins when ordering a queue."""
        return {"low": 0, "normal": 1, "high": 2, "emergency": 3}[self.value]


class QueueType(str, enum.Enum):
    HOSPITAL_SPECIFIC = "hospital_specific"
    CROSS_HOSPITAL = "cross_hospital"


class DoctorAvailabilityStatus(str, enum.Enum):
    AVAILABLE = "available"
    BUSY = "busy"
    BREAK = "break"
    OFFLINE = "offline"
    EMERGENCY = "emergency"


class UrgencyLevel(str, enum.Enum):
    EMERGENCY = "emergency"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ConsentStatus(str, enum.Enum):
    # Uppercase on the wire: the consent-management page compares 'GRANTED' directly
    # while the consultation page lowercases first, so uppercase satisfies both.
    GRANTED = "GRANTED"
    PENDING = "PENDING"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class ConsentType(str, enum.Enum):
    SPECIFIC_CONSULTATION = "specific_consultation"
    ONGOING_CARE = "ongoing_care"
    EMERGENCY = "emergency"


class DocumentStatus(str, enum.Enum):
    # Uppercase on the wire - medical-history/page.tsx compares uppercase.
    UPLOADED = "UPLOADED"
    VERIFIED = "VERIFIED"
    PROCESSING = "PROCESSING"
    REJECTED = "REJECTED"


class DocumentType(str, enum.Enum):
    LAB_REPORT = "lab_report"
    XRAY = "xray"
    MRI_SCAN = "mri_scan"
    CT_SCAN = "ct_scan"
    ULTRASOUND = "ultrasound"
    PRESCRIPTION = "prescription"
    MEDICAL_CERTIFICATE = "medical_certificate"
    DISCHARGE_SUMMARY = "discharge_summary"
    VACCINATION_RECORD = "vaccination_record"
    ALLERGY_REPORT = "allergy_report"
    SURGICAL_REPORT = "surgical_report"
    OTHER = "other"


class LeaveType(str, enum.Enum):
    VACATION = "vacation"
    SICK = "sick"
    CONFERENCE = "conference"
    EMERGENCY = "emergency"
