"""Consent enforcement.

The security-critical surface: a doctor must see exactly what the patient
shared and nothing else, regardless of what the client asks for.
"""

from datetime import UTC, date, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select

from src.core.security import hash_password
from src.models.clinical import LabReport, MedicalRecord, Medicine, Prescription, PrescriptionMedicine
from src.models.consent import ConsentAuditLog, MedicalDocument
from src.models.enums import (
    DocumentStatus,
    DocumentType,
    Gender,
    QueuePriority,
    QueueStatus,
    QueueType,
    TestStatus,
    UserRole,
)
from src.models.organizations import DiagnosticCenter, Hospital
from src.models.people import Doctor, Patient
from src.models.queue import QueueEntry
from src.models.user import User

_HASH = hash_password("Password123")


@pytest_asyncio.fixture
async def chart(session_factory):
    """A patient mid-consultation with history in every category."""
    async with session_factory() as db:
        hospital = Hospital(
            name="Chart Hospital",
            address="Road",
            phone="+910000000000",
            email="chart@example.com",
            license_number="H-CHART",
            latitude=12.97,
            longitude=77.60,
        )
        db.add(hospital)
        await db.flush()

        doc_user = User(
            email="chart.doc@example.com",
            username="chart.doc",
            full_name="Dr Chart",
            hashed_password=_HASH,
            role=UserRole.DOCTOR,
        )
        other_user = User(
            email="other.doc@example.com",
            username="other.doc",
            full_name="Dr Other",
            hashed_password=_HASH,
            role=UserRole.DOCTOR,
        )
        pat_user = User(
            email="chart.pat@example.com",
            username="chart.pat",
            full_name="Chart Patient",
            hashed_password=_HASH,
            role=UserRole.PATIENT,
        )
        db.add_all([doc_user, other_user, pat_user])
        await db.flush()

        doctor = Doctor(
            user_id=doc_user.id,
            hospital_id=hospital.id,
            license_number="D-CHART",
            specialization="General Medicine",
            years_of_experience=5,
            qualification="MBBS",
        )
        other_doctor = Doctor(
            user_id=other_user.id,
            hospital_id=hospital.id,
            license_number="D-OTHER",
            specialization="Cardiology",
            years_of_experience=5,
            qualification="MBBS",
        )
        patient = Patient(
            user_id=pat_user.id,
            uhid="ML2599999999",
            first_name="Chart",
            last_name="Patient",
            date_of_birth=date(1990, 5, 5),
            gender=Gender.FEMALE,
            phone="+919999999999",
            address="Somewhere",
            medical_allergies="Penicillin",
            chronic_conditions="Asthma",
            emergency_contact_name="Next Of Kin",
            emergency_contact_phone="+918888888888",
        )
        db.add_all([doctor, other_doctor, patient])
        await db.flush()

        centre_user = User(
            email="chart.lab@example.com",
            username="chart.lab",
            full_name="Chart Lab",
            hashed_password=_HASH,
            role=UserRole.DIAGNOSTIC_CENTER,
        )
        db.add(centre_user)
        await db.flush()
        centre = DiagnosticCenter(
            user_id=centre_user.id,
            name="Chart Lab",
            license_number="DGC-CHART",
            address="Lab Road",
            phone="+910000000001",
            email="chart.lab@example.com",
        )
        db.add(centre)
        await db.flush()

        db.add(
            MedicalRecord(
                patient_id=patient.id,
                doctor_id=other_doctor.id,
                chief_complaint="Old complaint",
                diagnosis="Old diagnosis",
            )
        )
        medicine = Medicine(name="Testicillin", manufacturer="Acme", strength="10mg", form="tablet")
        db.add(medicine)
        await db.flush()
        prescription = Prescription(patient_id=patient.id, doctor_id=other_doctor.id)
        db.add(prescription)
        await db.flush()
        db.add(
            PrescriptionMedicine(
                prescription_id=prescription.id,
                medicine_id=medicine.id,
                dosage="1-0-1",
                frequency="Twice daily",
                duration="5 days",
                quantity_prescribed=10,
            )
        )
        db.add(
            LabReport(
                patient_id=patient.id,
                diagnostic_center_id=centre.id,
                test_name="CBC",
                test_type="Pathology",
                results="Normal",
                status=TestStatus.COMPLETED,
            )
        )
        for doc_type in (DocumentType.XRAY, DocumentType.LAB_REPORT):
            db.add(
                MedicalDocument(
                    patient_id=patient.id,
                    file_name=f"{doc_type.value}.pdf",
                    storage_key=f"k/{doc_type.value}",
                    content_type="application/pdf",
                    file_size=1024,
                    document_type=doc_type,
                    status=DocumentStatus.UPLOADED,
                )
            )

        entry = QueueEntry(
            patient_id=patient.id,
            doctor_id=doctor.id,
            hospital_id=hospital.id,
            queue_type=QueueType.CROSS_HOSPITAL,
            queue_number=1,
            status=QueueStatus.IN_CONSULTATION,
            priority=QueuePriority.NORMAL,
        )
        db.add(entry)
        await db.commit()

        return {
            "queue_id": entry.id,
            "patient_id": patient.id,
            "doctor_id": doctor.id,
            "other_doctor_id": other_doctor.id,
            "hospital_id": hospital.id,
        }


@pytest_asyncio.fixture
async def dh(client, chart):
    login = await client.post(
        "/api/auth/login", json={"email": "chart.doc@example.com", "password": "Password123"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest_asyncio.fixture
async def other_dh(client, chart):
    login = await client.post(
        "/api/auth/login", json={"email": "other.doc@example.com", "password": "Password123"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest_asyncio.fixture
async def ph(client, chart):
    login = await client.post(
        "/api/auth/login", json={"email": "chart.pat@example.com", "password": "Password123"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _consent(**overrides):
    body = {
        "consent_type": "specific_consultation",
        "allow_medical_history": False,
        "allow_lab_reports": False,
        "allow_prescriptions": False,
        "allow_uploaded_documents": False,
        "allow_allergies": False,
        "allow_chronic_conditions": False,
        "allow_emergency_contact": False,
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------
# Without consent
# --------------------------------------------------------------------------
async def test_no_consent_means_no_history(client, chart, dh):
    resp = await client.get(f"/api/consultation/{chart['queue_id']}/medical-history", headers=dh)
    assert resp.status_code == 403
    assert "not shared" in resp.json()["detail"].lower()


async def test_no_consent_blocks_every_category(client, chart, dh):
    for path in ("medical-history", "prescriptions", "lab-reports", "documents"):
        resp = await client.get(f"/api/consultation/{chart['queue_id']}/{path}", headers=dh)
        assert resp.status_code == 403, path


async def test_identity_is_available_without_consent_but_not_sensitive_fields(client, chart, dh):
    """A doctor needs to know who they are treating; allergies and next-of-kin
    are separately consented."""
    body = (await client.get(f"/api/consultation/patient/{chart['queue_id']}", headers=dh)).json()
    assert body["uhid"] == "ML2599999999"
    assert body["medical_allergies"] is None
    assert body["chronic_conditions"] is None
    assert body["emergency_contact_phone"] is None


async def test_consent_endpoint_reports_nothing_shared(client, chart, dh):
    body = (await client.get(f"/api/consultation/{chart['queue_id']}/consent", headers=dh)).json()
    assert body == {"consents": []}


# --------------------------------------------------------------------------
# Selective consent
# --------------------------------------------------------------------------
async def test_prescriptions_only_consent_permits_exactly_that(client, chart, dh, ph):
    """The headline guarantee: sharing prescriptions must not open history."""
    created = await client.post(
        "/api/patients/me/consents",
        json=_consent(consultation_id=chart["queue_id"], allow_prescriptions=True),
        headers=ph,
    )
    assert created.status_code == 201, created.text

    allowed = await client.get(f"/api/consultation/{chart['queue_id']}/prescriptions", headers=dh)
    assert allowed.status_code == 200
    assert len(allowed.json()["prescriptions"]) == 1

    for blocked_path in ("medical-history", "lab-reports", "documents"):
        blocked = await client.get(f"/api/consultation/{chart['queue_id']}/{blocked_path}", headers=dh)
        assert blocked.status_code == 403, blocked_path


async def test_allergies_appear_only_when_shared(client, chart, dh, ph):
    await client.post(
        "/api/patients/me/consents",
        json=_consent(consultation_id=chart["queue_id"], allow_allergies=True),
        headers=ph,
    )
    body = (await client.get(f"/api/consultation/patient/{chart['queue_id']}", headers=dh)).json()
    assert body["medical_allergies"] == "Penicillin"
    assert body["chronic_conditions"] is None


async def test_document_type_allow_list_narrows_the_category(client, chart, dh, ph):
    await client.post(
        "/api/patients/me/consents",
        json=_consent(
            consultation_id=chart["queue_id"],
            allow_uploaded_documents=True,
            allowed_document_types=["xray"],
        ),
        headers=ph,
    )
    body = (await client.get(f"/api/consultation/{chart['queue_id']}/documents", headers=dh)).json()
    assert [d["document_type"] for d in body["documents"]] == ["xray"]


async def test_excluded_documents_are_withheld(client, chart, dh, ph, session_factory):
    async with session_factory() as db:
        docs = (await db.execute(select(MedicalDocument))).scalars().all()
        excluded = docs[0].id

    await client.post(
        "/api/patients/me/consents",
        json=_consent(
            consultation_id=chart["queue_id"],
            allow_uploaded_documents=True,
            excluded_document_ids=[excluded],
        ),
        headers=ph,
    )
    body = (await client.get(f"/api/consultation/{chart['queue_id']}/documents", headers=dh)).json()
    assert excluded not in [d["id"] for d in body["documents"]]
    assert len(body["documents"]) == 1


# --------------------------------------------------------------------------
# Revocation and expiry
# --------------------------------------------------------------------------
async def test_revoking_consent_closes_access_immediately(client, chart, dh, ph):
    created = await client.post(
        "/api/patients/me/consents",
        json=_consent(consultation_id=chart["queue_id"], allow_medical_history=True),
        headers=ph,
    )
    consent_id = created.json()["id"]

    assert (
        await client.get(f"/api/consultation/{chart['queue_id']}/medical-history", headers=dh)
    ).status_code == 200

    revoked = await client.put(f"/api/patients/me/consents/{consent_id}/revoke", headers=ph)
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "REVOKED"

    assert (
        await client.get(f"/api/consultation/{chart['queue_id']}/medical-history", headers=dh)
    ).status_code == 403


async def test_an_expired_consent_does_not_grant_access(client, chart, dh, ph, session_factory):
    from src.models.consent import PatientConsent

    created = await client.post(
        "/api/patients/me/consents",
        json=_consent(consultation_id=chart["queue_id"], allow_lab_reports=True),
        headers=ph,
    )
    consent_id = created.json()["id"]

    async with session_factory() as db:
        consent = await db.get(PatientConsent, consent_id)
        consent.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()

    resp = await client.get(f"/api/consultation/{chart['queue_id']}/lab-reports", headers=dh)
    assert resp.status_code == 403


async def test_listing_consents_marks_lapsed_ones_expired(client, chart, ph, session_factory):
    from src.models.consent import PatientConsent

    created = await client.post(
        "/api/patients/me/consents",
        json=_consent(consultation_id=chart["queue_id"], allow_lab_reports=True),
        headers=ph,
    )
    async with session_factory() as db:
        consent = await db.get(PatientConsent, created.json()["id"])
        consent.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()

    listed = (await client.get("/api/patients/me/consents", headers=ph)).json()
    assert listed[0]["status"] == "EXPIRED"


async def test_a_consent_expiring_in_the_past_is_rejected(client, chart, ph):
    resp = await client.post(
        "/api/patients/me/consents",
        json=_consent(
            consultation_id=chart["queue_id"],
            allow_lab_reports=True,
            expires_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        ),
        headers=ph,
    )
    assert resp.status_code == 422


async def test_a_consent_sharing_nothing_is_rejected(client, chart, ph):
    """Almost always a UI mistake, and storing it leaves the patient believing
    they shared something."""
    resp = await client.post(
        "/api/patients/me/consents", json=_consent(consultation_id=chart["queue_id"]), headers=ph
    )
    assert resp.status_code == 422


async def test_revoking_twice_is_refused(client, chart, ph):
    created = await client.post(
        "/api/patients/me/consents",
        json=_consent(consultation_id=chart["queue_id"], allow_prescriptions=True),
        headers=ph,
    )
    consent_id = created.json()["id"]
    await client.put(f"/api/patients/me/consents/{consent_id}/revoke", headers=ph)
    again = await client.put(f"/api/patients/me/consents/{consent_id}/revoke", headers=ph)
    assert again.status_code == 409


# --------------------------------------------------------------------------
# Who may look at all
# --------------------------------------------------------------------------
async def test_a_doctor_not_treating_the_patient_is_refused(client, chart, other_dh, ph):
    """Even with a broad consent granted to someone else."""
    await client.post(
        "/api/patients/me/consents",
        json=_consent(consultation_id=chart["queue_id"], allow_medical_history=True),
        headers=ph,
    )
    resp = await client.get(f"/api/consultation/{chart['queue_id']}/medical-history", headers=other_dh)
    assert resp.status_code == 403


async def test_access_ends_when_the_consultation_ends(client, chart, dh, ph, session_factory):
    """A finished consultation must not leave a standing key to the record."""
    await client.post(
        "/api/patients/me/consents",
        json=_consent(consultation_id=chart["queue_id"], allow_medical_history=True),
        headers=ph,
    )
    assert (
        await client.get(f"/api/consultation/{chart['queue_id']}/medical-history", headers=dh)
    ).status_code == 200

    async with session_factory() as db:
        entry = await db.get(QueueEntry, chart["queue_id"])
        entry.status = QueueStatus.COMPLETED
        await db.commit()

    resp = await client.get(f"/api/consultation/{chart['queue_id']}/medical-history", headers=dh)
    assert resp.status_code == 403
    assert "ended" in resp.json()["detail"].lower()


async def test_a_patient_cannot_open_the_doctor_chart(client, chart, ph):
    resp = await client.get(f"/api/consultation/{chart['queue_id']}/medical-history", headers=ph)
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Scope resolution
# --------------------------------------------------------------------------
async def test_a_doctor_scoped_consent_covers_the_consultation(client, chart, dh, ph):
    await client.post(
        "/api/patients/me/consents",
        json=_consent(doctor_id=chart["doctor_id"], consent_type="ongoing_care", allow_prescriptions=True),
        headers=ph,
    )
    resp = await client.get(f"/api/consultation/{chart['queue_id']}/prescriptions", headers=dh)
    assert resp.status_code == 200


async def test_a_narrow_consultation_consent_wins_over_a_broad_standing_one(client, chart, dh, ph):
    """Otherwise a broad standing grant silently widens a deliberately narrow
    per-consultation choice."""
    await client.post(
        "/api/patients/me/consents",
        json=_consent(
            consent_type="ongoing_care",
            allow_medical_history=True,
            allow_prescriptions=True,
            allow_lab_reports=True,
        ),
        headers=ph,
    )
    await client.post(
        "/api/patients/me/consents",
        json=_consent(consultation_id=chart["queue_id"], allow_prescriptions=True),
        headers=ph,
    )

    assert (
        await client.get(f"/api/consultation/{chart['queue_id']}/prescriptions", headers=dh)
    ).status_code == 200
    assert (
        await client.get(f"/api/consultation/{chart['queue_id']}/medical-history", headers=dh)
    ).status_code == 403


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
async def test_granted_access_is_audited(client, chart, dh, ph, session_factory):
    await client.post(
        "/api/patients/me/consents",
        json=_consent(consultation_id=chart["queue_id"], allow_prescriptions=True),
        headers=ph,
    )
    await client.get(f"/api/consultation/{chart['queue_id']}/prescriptions", headers=dh)

    async with session_factory() as db:
        logs = (await db.execute(select(ConsentAuditLog))).scalars().all()
    assert any(log.category == "prescriptions" and log.granted for log in logs)


async def test_denied_access_is_also_audited(client, chart, dh, session_factory):
    """A patient who cannot see who tried to open their record has not really
    been given control of it."""
    await client.get(f"/api/consultation/{chart['queue_id']}/medical-history", headers=dh)

    async with session_factory() as db:
        logs = (await db.execute(select(ConsentAuditLog))).scalars().all()
    denied = [log for log in logs if not log.granted]
    assert denied
    assert denied[0].reason == "no_consent"


# --------------------------------------------------------------------------
# Prescribing
# --------------------------------------------------------------------------
async def test_prescribing_accepts_free_text_medicine_names(client, chart, dh):
    """The Rx pad sends names, not ids -- refusing an unknown one would block a
    doctor mid-consultation over a spelling difference."""
    resp = await client.post(
        f"/api/consultation/{chart['queue_id']}/prescription",
        json={
            "medicines": [
                {
                    "medicine_name": "Something Entirely New",
                    "dosage": "1-0-1",
                    "frequency": "Twice daily",
                    "duration": "5 days",
                    "quantity_prescribed": 10,
                }
            ],
            "notes": "After food",
        },
        headers=dh,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["medicine_count"] == 1


async def test_prescribing_does_not_need_a_read_consent(client, chart, dh):
    """A doctor treating a patient may always prescribe, even if the patient
    shared no history."""
    resp = await client.post(
        f"/api/consultation/{chart['queue_id']}/prescription",
        json={
            "medicines": [
                {
                    "medicine_name": "Paracetamol",
                    "dosage": "1-1-1",
                    "frequency": "Thrice daily",
                    "duration": "3 days",
                    "quantity_prescribed": 9,
                }
            ]
        },
        headers=dh,
    )
    assert resp.status_code == 201


async def test_cannot_prescribe_against_a_finished_consultation(client, chart, dh, session_factory):
    async with session_factory() as db:
        entry = await db.get(QueueEntry, chart["queue_id"])
        entry.status = QueueStatus.COMPLETED
        await db.commit()

    resp = await client.post(
        f"/api/consultation/{chart['queue_id']}/prescription",
        json={
            "medicines": [
                {
                    "medicine_name": "Paracetamol",
                    "dosage": "1-0-1",
                    "frequency": "Twice",
                    "duration": "2 days",
                    "quantity_prescribed": 4,
                }
            ]
        },
        headers=dh,
    )
    assert resp.status_code == 409


async def test_medicine_search_finds_catalogue_entries(client, chart, dh):
    resp = await client.get("/api/consultation/medicines/search", params={"query": "testi"}, headers=dh)
    assert resp.status_code == 200
    assert any(m["name"] == "Testicillin" for m in resp.json())


# --------------------------------------------------------------------------
# Standing access outside a live consultation: any active grant that names
# the doctor or a hospital they practise at is honoured, not ongoing-care only.
# --------------------------------------------------------------------------
async def test_specific_consultation_grant_to_this_doctor_allows_standing_read(client, chart, dh, ph):
    await client.post(
        "/api/patients/me/consents",
        json=_consent(doctor_id=chart["doctor_id"], allow_medical_history=True),
        headers=ph,
    )
    pid = chart["patient_id"]

    records = await client.get(f"/api/doctor/patients/id/{pid}/medical-records", headers=dh)
    assert records.status_code == 200, records.text
    assert len(records.json()) == 1

    summary = await client.get(f"/api/doctor/patients/id/{pid}/summary", headers=dh)
    assert summary.status_code == 200

    # A category the grant does not cover is still refused.
    labs = await client.get(f"/api/doctor/patients/id/{pid}/lab-reports", headers=dh)
    assert labs.status_code == 403


async def test_hospital_scoped_grant_allows_standing_read(client, chart, dh, ph):
    await client.post(
        "/api/patients/me/consents",
        json=_consent(hospital_id=chart["hospital_id"], allow_lab_reports=True),
        headers=ph,
    )
    resp = await client.get(
        f"/api/doctor/patients/id/{chart['patient_id']}/lab-reports", headers=dh
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


async def test_grant_naming_another_doctor_does_not_leak(client, chart, dh, ph):
    await client.post(
        "/api/patients/me/consents",
        json=_consent(doctor_id=chart["other_doctor_id"], allow_medical_history=True),
        headers=ph,
    )
    resp = await client.get(
        f"/api/doctor/patients/id/{chart['patient_id']}/medical-records", headers=dh
    )
    assert resp.status_code == 403


async def test_revoked_standing_grant_is_refused(client, chart, dh, ph):
    created = await client.post(
        "/api/patients/me/consents",
        json=_consent(doctor_id=chart["doctor_id"], allow_medical_history=True),
        headers=ph,
    )
    consent_id = created.json()["id"]
    pid = chart["patient_id"]
    assert (
        await client.get(f"/api/doctor/patients/id/{pid}/medical-records", headers=dh)
    ).status_code == 200

    await client.put(f"/api/patients/me/consents/{consent_id}/revoke", headers=ph)
    assert (
        await client.get(f"/api/doctor/patients/id/{pid}/medical-records", headers=dh)
    ).status_code == 403
