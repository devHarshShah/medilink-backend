"""Consent, document and consultation-chart schemas.

Shapes taken from consent-management/page.tsx, medical-history/page.tsx and
consultation/[queueId]/page.tsx. Statuses are uppercase on the wire because the
consent list compares 'GRANTED' directly while the consultation page lowercases
first -- uppercase satisfies both.
"""

from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from src.models.enums import ConsentType, DocumentType


class ConsentCreate(BaseModel):
    doctor_id: int | None = None
    hospital_id: int | None = None
    consultation_id: int | None = None
    consent_type: ConsentType
    purpose: str | None = None

    allow_medical_history: bool = False
    allow_lab_reports: bool = False
    allow_prescriptions: bool = False
    allow_uploaded_documents: bool = False
    allow_allergies: bool = False
    allow_chronic_conditions: bool = False
    allow_emergency_contact: bool = False

    allowed_document_types: list[str] | None = None
    excluded_document_ids: list[int] | None = None
    expires_at: datetime | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def at_least_one_category(self):
        flags = [
            self.allow_medical_history,
            self.allow_lab_reports,
            self.allow_prescriptions,
            self.allow_uploaded_documents,
            self.allow_allergies,
            self.allow_chronic_conditions,
            self.allow_emergency_contact,
        ]
        if not any(flags):
            # A consent that shares nothing is almost always a UI mistake, and
            # silently storing it leaves the patient believing they shared.
            raise ValueError("Select at least one category to share")
        return self


class ConsentResponse(BaseModel):
    id: int
    patient_id: int
    doctor_id: int | None = None
    doctor_name: str | None = None
    hospital_id: int | None = None
    hospital_name: str | None = None
    consultation_id: int | None = None
    consent_type: str
    purpose: str | None = None

    allow_medical_history: bool
    allow_lab_reports: bool
    allow_prescriptions: bool
    allow_uploaded_documents: bool
    allow_allergies: bool
    allow_chronic_conditions: bool
    allow_emergency_contact: bool

    allowed_document_types: list[str] | None = None
    excluded_document_ids: list[int] | None = None

    # Uppercase on the wire -- see the module docstring.
    status: str
    granted_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    notes: str | None = None


class ConsentsEnvelope(BaseModel):
    consents: list[ConsentResponse]


class MedicalDocumentResponse(BaseModel):
    id: int
    file_name: str
    document_type: str
    description: str | None = None
    document_date: datetime | None = None
    doctor_name: str | None = None
    hospital_name: str | None = None
    status: str
    file_size: int
    created_at: datetime


class DocumentsEnvelope(BaseModel):
    documents: list[MedicalDocumentResponse]


# --------------------------------------------------------------------------
# Consultation chart
# --------------------------------------------------------------------------
class ConsultationPatientInfo(BaseModel):
    """Returned bare, not enveloped -- the consultation page reads it directly."""

    id: int
    uhid: str
    first_name: str
    last_name: str
    date_of_birth: date
    gender: str
    blood_group: str | None = None
    phone: str
    # Only populated when the matching consent flag is set.
    medical_allergies: str | None = None
    chronic_conditions: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None


class ConsultationMedicalRecord(BaseModel):
    id: int
    chief_complaint: str
    diagnosis: str
    treatment_plan: str | None = None
    notes: str | None = None
    vitals: str | None = None
    visit_date: datetime
    doctor_name: str | None = None


class MedicalHistoryEnvelope(BaseModel):
    medical_records: list[ConsultationMedicalRecord]


class ConsultationPrescriptionMedicine(BaseModel):
    medicine_name: str
    dosage: str
    frequency: str
    duration: str
    instructions: str | None = None
    quantity_prescribed: int


class ConsultationPrescription(BaseModel):
    id: int
    prescription_date: datetime
    status: str
    notes: str | None = None
    follow_up_date: date | None = None
    doctor_name: str | None = None
    medicines: list[ConsultationPrescriptionMedicine]


class PrescriptionsEnvelope(BaseModel):
    prescriptions: list[ConsultationPrescription]


class ConsultationLabReport(BaseModel):
    id: int
    test_name: str
    test_type: str
    results: str | None = None
    reference_ranges: str | None = None
    interpretation: str | None = None
    status: str
    requested_date: datetime
    completed_date: datetime | None = None


class LabReportsEnvelope(BaseModel):
    lab_reports: list[ConsultationLabReport]


class ConsultationMedicineLine(BaseModel):
    # Free text, not an id: the Rx pad sends names and the catalogue is
    # resolve-or-create behind it.
    medicine_name: str = Field(min_length=1, max_length=255)
    dosage: str
    frequency: str
    duration: str
    instructions: str | None = None
    quantity_prescribed: int = Field(ge=1, le=1000)


class ConsultationPrescriptionCreate(BaseModel):
    medicines: list[ConsultationMedicineLine] = Field(min_length=1)
    notes: str | None = None
    follow_up_date: date | None = None


class MedicineSearchResult(BaseModel):
    id: int
    name: str
    generic_name: str | None = None
    strength: str
    form: str
    manufacturer: str


class LabRequestCreate(BaseModel):
    patient_id: int
    diagnostic_center_id: int
    test_name: str = Field(min_length=1, max_length=255)
    test_type: str = Field(min_length=1, max_length=120)
    notes: str | None = None


DOCUMENT_TYPES = [t.value for t in DocumentType]
