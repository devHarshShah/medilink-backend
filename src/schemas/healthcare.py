"""Core healthcare domain schemas.

Module path is load-bearing: openapi.json refers to
`src__schemas__healthcare__UserResponse`, so the frontend's generated types
only line up if this module keeps its name and its UserResponse.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.models.enums import AccessType, Gender, PrescriptionStatus, TestStatus, UserRole


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    # JSON body, not OAuth2 form-encoding - the frontend posts application/json.
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    username: str
    full_name: str | None = None
    role: UserRole
    is_active: bool
    created_at: datetime
    # Not in the original spec, but src/types/api.ts marks it required.
    updated_at: datetime | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
    expires_in: int


# --------------------------------------------------------------------------
# Patients
# --------------------------------------------------------------------------
class PatientBase(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    date_of_birth: date
    gender: Gender
    blood_group: str | None = None
    phone: str = Field(min_length=6, max_length=32)
    address: str
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    medical_allergies: str | None = None
    chronic_conditions: str | None = None


class PatientCreate(PatientBase):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    # The registration form also sends these two; accept them as aliases rather
    # than 422-ing a valid signup.
    allergies: str | None = None
    medical_history: str | None = None


class PatientResponse(PatientBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uhid: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Hospitals & staff
# --------------------------------------------------------------------------
class HospitalCreate(BaseModel):
    name: str
    address: str
    phone: str
    email: EmailStr
    license_number: str
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class HospitalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    address: str
    phone: str
    email: str
    license_number: str
    latitude: float | None = None
    longitude: float | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class DoctorCreate(BaseModel):
    license_number: str
    specialization: str
    years_of_experience: int = Field(ge=0, le=80)
    qualification: str
    consultation_fee: float | None = Field(default=None, ge=0)
    email: EmailStr
    full_name: str
    password: str = Field(min_length=8, max_length=128)
    hospital_id: int


class DoctorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    license_number: str
    specialization: str
    years_of_experience: int
    qualification: str
    consultation_fee: float | None = None
    hospital_id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
    full_name: str | None = None
    email: str | None = None


class NurseCreate(BaseModel):
    license_number: str
    department: str
    shift: str
    years_of_experience: int = Field(ge=0, le=80)
    email: EmailStr
    full_name: str
    password: str = Field(min_length=8, max_length=128)
    hospital_id: int


class NurseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    license_number: str
    department: str
    shift: str
    years_of_experience: int
    hospital_id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PharmacyCreate(BaseModel):
    name: str
    license_number: str
    address: str
    phone: str
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    latitude: float | None = None
    longitude: float | None = None


class PharmacyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    license_number: str
    address: str
    phone: str
    email: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class DiagnosticCenterCreate(BaseModel):
    name: str
    license_number: str
    address: str
    phone: str
    email: EmailStr
    services_offered: str | None = None
    password: str = Field(min_length=8, max_length=128)
    latitude: float | None = None
    longitude: float | None = None


class DiagnosticCenterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    license_number: str
    address: str
    phone: str
    email: str
    services_offered: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Clinical
# --------------------------------------------------------------------------
class MedicalRecordCreate(BaseModel):
    chief_complaint: str
    diagnosis: str
    treatment_plan: str | None = None
    notes: str | None = None
    vitals: str | None = None
    patient_id: int


class MedicalRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chief_complaint: str
    diagnosis: str
    treatment_plan: str | None = None
    notes: str | None = None
    vitals: str | None = None
    patient_id: int
    doctor_id: int
    visit_date: datetime
    created_at: datetime
    updated_at: datetime
    doctor_name: str | None = None


class MedicineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    generic_name: str | None = None
    manufacturer: str
    strength: str
    form: str
    price_per_unit: float | None = None
    is_active: bool
    created_at: datetime


class PrescriptionMedicineCreate(BaseModel):
    medicine_id: int
    dosage: str
    frequency: str
    duration: str
    instructions: str | None = None
    quantity_prescribed: int = Field(ge=1)


class PrescriptionMedicineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    medicine_id: int
    dosage: str
    frequency: str
    duration: str
    instructions: str | None = None
    quantity_prescribed: int
    created_at: datetime
    medicine: MedicineResponse
    medicine_name: str | None = None


class PrescriptionCreate(BaseModel):
    patient_id: int
    medicines: list[PrescriptionMedicineCreate]
    notes: str | None = None
    follow_up_date: date | None = None


class PrescriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    doctor_id: int
    prescription_date: datetime
    status: PrescriptionStatus
    notes: str | None = None
    follow_up_date: date | None = None
    created_at: datetime
    updated_at: datetime
    medicines: list[PrescriptionMedicineResponse] = []
    doctor_name: str | None = None


class LabReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    test_name: str
    test_type: str
    results: str | None = None
    reference_ranges: str | None = None
    interpretation: str | None = None
    technician_notes: str | None = None
    patient_id: int
    diagnostic_center_id: int
    requested_date: datetime
    completed_date: datetime | None = None
    status: TestStatus
    report_file_path: str | None = None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# QR access
# --------------------------------------------------------------------------
class QRAccessRequest(BaseModel):
    access_type: AccessType
    # The prescriptions page sends 0.5, so this must be a float. `expires_minutes`
    # is kept for compatibility with the older spec.
    duration_hours: float | None = Field(default=None, gt=0, le=168)
    expires_minutes: int | None = Field(default=None, gt=0, le=10080)

    @property
    def ttl_minutes(self) -> int:
        if self.duration_hours is not None:
            return max(1, round(self.duration_hours * 60))
        if self.expires_minutes is not None:
            return self.expires_minutes
        return 30


class QRAccessResponse(BaseModel):
    # Base64 PNG with NO data-URI prefix: the UI builds
    # `data:image/png;base64,${qr_code}` itself.
    qr_code: str
    access_token: str
    expires_at: datetime
    access_type: AccessType
