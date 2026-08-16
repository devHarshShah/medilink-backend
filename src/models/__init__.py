"""SQLAlchemy models.

Every model must be imported here so Alembic's autogenerate and Base.metadata
see the full schema.
"""

from src.db.base import Base
from src.models.access import QRAccessGrant
from src.models.clinical import (
    LabReport,
    MedicalRecord,
    Medicine,
    Prescription,
    PrescriptionMedicine,
)
from src.models.consent import ConsentAuditLog, MedicalDocument, PatientConsent
from src.models.doctor_ops import DoctorCheckIn, DoctorLeave, DoctorSchedule
from src.models.enums import (
    AccessType,
    ConsentStatus,
    ConsentType,
    DoctorAvailabilityStatus,
    DocumentStatus,
    DocumentType,
    Gender,
    LeaveType,
    PrescriptionStatus,
    QueuePriority,
    QueueStatus,
    QueueType,
    TestStatus,
    UrgencyLevel,
    UserRole,
)
from src.models.organizations import DiagnosticCenter, Hospital, Pharmacy
from src.models.people import Doctor, Nurse, Patient
from src.models.queue import QueueEntry
from src.models.triage import TriageAnalysis
from src.models.user import User

__all__ = [
    "AccessType",
    "Base",
    "ConsentAuditLog",
    "ConsentStatus",
    "ConsentType",
    "DiagnosticCenter",
    "Doctor",
    "DoctorAvailabilityStatus",
    "DoctorCheckIn",
    "DoctorLeave",
    "DoctorSchedule",
    "DocumentStatus",
    "DocumentType",
    "Gender",
    "Hospital",
    "LabReport",
    "LeaveType",
    "MedicalDocument",
    "MedicalRecord",
    "Medicine",
    "Nurse",
    "Patient",
    "PatientConsent",
    "Pharmacy",
    "Prescription",
    "PrescriptionMedicine",
    "PrescriptionStatus",
    "QRAccessGrant",
    "QueueEntry",
    "QueuePriority",
    "QueueStatus",
    "QueueType",
    "TestStatus",
    "TriageAnalysis",
    "UrgencyLevel",
    "User",
    "UserRole",
]
