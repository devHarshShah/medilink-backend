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
from src.models.user import User

__all__ = [
    "AccessType",
    "Base",
    "ConsentStatus",
    "ConsentType",
    "DiagnosticCenter",
    "Doctor",
    "DoctorAvailabilityStatus",
    "DocumentStatus",
    "DocumentType",
    "Gender",
    "Hospital",
    "LabReport",
    "LeaveType",
    "MedicalRecord",
    "Medicine",
    "Nurse",
    "Patient",
    "Pharmacy",
    "Prescription",
    "PrescriptionMedicine",
    "PrescriptionStatus",
    "QRAccessGrant",
    "QueuePriority",
    "QueueStatus",
    "QueueType",
    "TestStatus",
    "UrgencyLevel",
    "User",
    "UserRole",
]
