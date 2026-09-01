"""Family doctor + patient-controlled standing access."""


import pytest_asyncio
from sqlalchemy import select

from src.core.security import hash_password
from src.models.clinical import LabReport, MedicalRecord
from src.models.enums import TestStatus, UserRole
from src.models.organizations import DiagnosticCenter, Hospital
from src.models.people import Doctor, Patient
from src.models.user import User

_HASH = hash_password("Password123")


@pytest_asyncio.fixture
async def patient_headers(client, patient_payload):
    await client.post("/api/auth/register/patient", json=patient_payload)
    login = await client.post(
        "/api/auth/login",
        json={"email": patient_payload["email"], "password": patient_payload["password"]},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest_asyncio.fixture
async def world(client, patient_headers, session_factory):
    """A registered patient, a doctor with a login, and some clinical history."""
    async with session_factory() as db:
        patient = (
            await db.execute(select(Patient).join(User).where(User.email == "test.patient@example.com"))
        ).scalar_one()

        hospital = Hospital(
            name="Fam Hospital",
            address="Road",
            phone="+910000000000",
            email="fam@example.com",
            license_number="H-FAM",
        )
        db.add(hospital)
        await db.flush()

        doc_user = User(
            email="fam.doc@example.com",
            username="fam.doc",
            full_name="Dr Family",
            hashed_password=_HASH,
            role=UserRole.DOCTOR,
        )
        dgc_user = User(
            email="fam.dgc.user@example.com",
            username="fam.dgc.user",
            full_name="Fam Diagnostics",
            hashed_password=_HASH,
            role=UserRole.DIAGNOSTIC_CENTER,
        )
        db.add_all([doc_user, dgc_user])
        await db.flush()
        doctor = Doctor(
            user_id=doc_user.id,
            hospital_id=hospital.id,
            license_number="D-FAM",
            specialization="General Medicine",
            years_of_experience=10,
            qualification="MBBS",
        )
        db.add(doctor)
        await db.flush()

        center = DiagnosticCenter(
            user_id=dgc_user.id,
            name="Fam Diagnostics",
            license_number="DGC-FAM",
            address="Road",
            phone="+910000000000",
            email="fam.dgc@example.com",
        )
        db.add(center)
        await db.flush()

        db.add(
            MedicalRecord(
                patient_id=patient.id,
                doctor_id=doctor.id,
                chief_complaint="Checkup",
                diagnosis="Healthy",
                treatment_plan="None",
            )
        )
        db.add(
            LabReport(
                patient_id=patient.id,
                diagnostic_center_id=center.id,
                test_name="CBC",
                test_type="Pathology",
                status=TestStatus.COMPLETED,
            )
        )
        await db.commit()
        return {"patient_id": patient.id, "doctor_id": doctor.id}


@pytest_asyncio.fixture
async def doc_headers(client, world):
    resp = await client.post(
        "/api/auth/login", json={"email": "fam.doc@example.com", "password": "Password123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_set_family_doctor_records_the_id_and_name(client, world, patient_headers):
    resp = await client.put(
        "/api/patients/me/family-doctor",
        json={"doctor_id": world["doctor_id"], "allow_medical_history": True},
        headers=patient_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["family_doctor_id"] == world["doctor_id"]
    assert body["family_doctor_name"] == "Dr Family"

    me = (await client.get("/api/patients/me", headers=patient_headers)).json()
    assert me["family_doctor_name"] == "Dr Family"


async def test_set_family_doctor_needs_at_least_one_category(client, world, patient_headers):
    resp = await client.put(
        "/api/patients/me/family-doctor",
        json={"doctor_id": world["doctor_id"]},
        headers=patient_headers,
    )
    assert resp.status_code == 422


async def test_family_doctor_can_read_only_the_shared_categories(client, world, patient_headers, doc_headers):
    await client.put(
        "/api/patients/me/family-doctor",
        json={
            "doctor_id": world["doctor_id"],
            "allow_medical_history": True,
            "allow_lab_reports": True,
            "allow_uploaded_documents": False,
        },
        headers=patient_headers,
    )
    pid = world["patient_id"]

    records = await client.get(f"/api/doctor/patients/id/{pid}/medical-records", headers=doc_headers)
    assert records.status_code == 200
    assert len(records.json()) == 1

    labs = await client.get(f"/api/doctor/patients/id/{pid}/lab-reports", headers=doc_headers)
    assert labs.status_code == 200
    assert len(labs.json()) == 1

    docs = await client.get(f"/api/doctor/patients/id/{pid}/documents", headers=doc_headers)
    assert docs.status_code == 403


async def test_doctor_without_a_grant_is_denied(client, world, doc_headers):
    pid = world["patient_id"]
    # No family-doctor set yet.
    assert (
        await client.get(f"/api/doctor/patients/id/{pid}/medical-records", headers=doc_headers)
    ).status_code == 403
    # The previously-open leak is now closed too.
    assert (
        await client.get(f"/api/doctor/medical-records/patient/{pid}", headers=doc_headers)
    ).status_code == 403


async def test_family_doctor_appears_in_my_patients_with_a_badge(client, world, patient_headers, doc_headers):
    await client.put(
        "/api/patients/me/family-doctor",
        json={"doctor_id": world["doctor_id"], "allow_prescriptions": True},
        headers=patient_headers,
    )
    rows = (await client.get("/api/doctor/my-patients", headers=doc_headers)).json()
    mine = [r for r in rows if r["id"] == world["patient_id"]]
    assert mine and mine[0]["is_family_patient"] is True


async def test_clearing_the_family_doctor_revokes_access(client, world, patient_headers, doc_headers):
    pid = world["patient_id"]
    await client.put(
        "/api/patients/me/family-doctor",
        json={"doctor_id": world["doctor_id"], "allow_medical_history": True},
        headers=patient_headers,
    )
    assert (
        await client.get(f"/api/doctor/patients/id/{pid}/medical-records", headers=doc_headers)
    ).status_code == 200

    cleared = await client.delete("/api/patients/me/family-doctor", headers=patient_headers)
    assert cleared.status_code == 200
    assert cleared.json()["family_doctor_id"] is None

    assert (
        await client.get(f"/api/doctor/patients/id/{pid}/medical-records", headers=doc_headers)
    ).status_code == 403
