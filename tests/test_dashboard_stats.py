"""Admin and nurse dashboard stats endpoints."""

from datetime import UTC, date, datetime

import pytest_asyncio

from src.core.security import hash_password
from src.models.enums import Gender, QueueStatus, QueueType, UserRole
from src.models.organizations import Hospital
from src.models.people import Doctor, Nurse, Patient
from src.models.queue import QueueEntry
from src.models.user import User

_HASH = hash_password("Password123")


@pytest_asyncio.fixture
async def hospital_world(session_factory):
    async with session_factory() as db:
        hospital = Hospital(
            name="Stat Hospital",
            address="Road",
            phone="+910000000000",
            email="stat@example.com",
            license_number="H-STAT",
        )
        db.add(hospital)
        await db.flush()

        doc_user = User(
            email="stat.doc@example.com",
            username="stat.doc",
            full_name="Dr Stat",
            hashed_password=_HASH,
            role=UserRole.DOCTOR,
        )
        nurse_user = User(
            email="stat.nurse@example.com",
            username="stat.nurse",
            full_name="Nurse Stat",
            hashed_password=_HASH,
            role=UserRole.NURSE,
        )
        pat_user = User(
            email="stat.pat@example.com",
            username="stat.pat",
            full_name="Stat Patient",
            hashed_password=_HASH,
            role=UserRole.PATIENT,
        )
        db.add_all([doc_user, nurse_user, pat_user])
        await db.flush()

        doctor = Doctor(
            user_id=doc_user.id,
            hospital_id=hospital.id,
            license_number="D-STAT",
            specialization="General Medicine",
            years_of_experience=5,
            qualification="MBBS",
        )
        nurse = Nurse(
            user_id=nurse_user.id,
            hospital_id=hospital.id,
            license_number="N-STAT",
            department="OPD",
            shift="Morning",
            years_of_experience=3,
        )
        patient = Patient(
            user_id=pat_user.id,
            uhid="ML25STAT0001",
            first_name="Stat",
            last_name="Patient",
            date_of_birth=date(1990, 1, 1),
            gender=Gender.MALE,
            phone="+910000000000",
            address="Road",
        )
        db.add_all([doctor, nurse, patient])
        await db.flush()

        db.add(
            QueueEntry(
                patient_id=patient.id,
                doctor_id=doctor.id,
                hospital_id=hospital.id,
                queue_type=QueueType.HOSPITAL_SPECIFIC,
                queue_number=1,
                status=QueueStatus.COMPLETED,
                consultation_completed_at=datetime.now(UTC),
            )
        )
        db.add(
            QueueEntry(
                patient_id=patient.id,
                doctor_id=doctor.id,
                hospital_id=hospital.id,
                queue_type=QueueType.HOSPITAL_SPECIFIC,
                queue_number=2,
                status=QueueStatus.WAITING,
            )
        )
        await db.commit()
        return {"hospital_id": hospital.id}


async def _login(client, email):
    resp = await client.post("/api/auth/login", json={"email": email, "password": "Password123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_admin_stats_report_real_counts(client, admin_headers, hospital_world):
    body = (await client.get("/api/admin/stats", headers=admin_headers)).json()
    assert body["hospitals"] >= 1
    assert body["doctors"] >= 1
    assert body["nurses"] >= 1
    assert body["patients"] >= 1
    assert body["consultations_today"] == 1
    assert body["patients_waiting"] == 1
    assert body["registrations_this_week"] >= 1


async def test_admin_stats_reject_non_admin(client, hospital_world):
    headers = await _login(client, "stat.doc@example.com")
    assert (await client.get("/api/admin/stats", headers=headers)).status_code == 403


async def test_nurse_stats_are_scoped_to_the_hospital(client, hospital_world):
    headers = await _login(client, "stat.nurse@example.com")
    body = (await client.get("/api/nurse/stats", headers=headers)).json()
    assert body["patients_waiting"] == 1
    assert body["completed_today"] == 1
    assert body["in_consultation"] == 0
    assert body["emergency_waiting"] == 0
    assert body["doctors_on_duty"] == 0  # nobody has checked in


async def test_nurse_stats_reject_non_nurse(client, admin_headers, hospital_world):
    assert (await client.get("/api/nurse/stats", headers=admin_headers)).status_code == 403
