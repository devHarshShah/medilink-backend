import base64

import pytest_asyncio


@pytest_asyncio.fixture
async def patient_headers(client, patient_payload):
    await client.post("/api/auth/register/patient", json=patient_payload)
    login = await client.post(
        "/api/auth/login",
        json={"email": patient_payload["email"], "password": patient_payload["password"]},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_qr_code_is_bare_base64_png(client, patient_headers):
    """The UI builds `data:image/png;base64,${qr_code}` itself. A data-URI prefix
    here would produce a doubled prefix and a broken image."""
    resp = await client.post(
        "/api/patients/me/qr-access",
        json={"access_type": "prescription_only", "duration_hours": 0.5},
        headers=patient_headers,
    )
    assert resp.status_code == 200, resp.text
    qr = resp.json()["qr_code"]
    assert not qr.startswith("data:")
    # Decodes as base64 and carries the PNG magic number.
    assert base64.b64decode(qr)[:8] == b"\x89PNG\r\n\x1a\n"


async def test_fractional_duration_hours_is_accepted(client, patient_headers):
    """The prescriptions page sends 0.5 - rejecting floats would break that screen."""
    resp = await client.post(
        "/api/patients/me/qr-access",
        json={"access_type": "prescription_only", "duration_hours": 0.5},
        headers=patient_headers,
    )
    assert resp.status_code == 200


async def test_qr_grant_redeems_to_the_right_patient(client, patient_headers):
    grant = await client.post(
        "/api/patients/me/qr-access",
        json={"access_type": "full_access", "duration_hours": 1},
        headers=patient_headers,
    )
    token = grant.json()["access_token"]

    resp = await client.post(f"/api/patients/qr-access/{token}")
    assert resp.status_code == 200
    assert resp.json()["first_name"] == "Test"


async def test_unknown_qr_token_is_rejected(client):
    resp = await client.post("/api/patients/qr-access/definitely-not-a-real-token")
    assert resp.status_code == 403


async def test_expired_qr_grant_is_rejected(client, patient_headers, session_factory):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from src.models.access import QRAccessGrant

    grant = await client.post(
        "/api/patients/me/qr-access",
        json={"access_type": "full_access", "duration_hours": 1},
        headers=patient_headers,
    )
    token = grant.json()["access_token"]

    async with session_factory() as db:
        row = (
            await db.execute(select(QRAccessGrant).where(QRAccessGrant.access_token == token))
        ).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()

    resp = await client.post(f"/api/patients/qr-access/{token}")
    assert resp.status_code == 403
    assert "expired" in resp.json()["detail"].lower()


async def test_revoked_qr_grant_is_rejected(client, patient_headers, session_factory):
    from sqlalchemy import select

    from src.models.access import QRAccessGrant

    grant = await client.post(
        "/api/patients/me/qr-access",
        json={"access_type": "full_access", "duration_hours": 1},
        headers=patient_headers,
    )
    token = grant.json()["access_token"]

    async with session_factory() as db:
        row = (
            await db.execute(select(QRAccessGrant).where(QRAccessGrant.access_token == token))
        ).scalar_one()
        row.is_revoked = True
        await db.commit()

    resp = await client.post(f"/api/patients/qr-access/{token}")
    assert resp.status_code == 403


async def test_reports_only_grant_does_not_unlock_prescriptions(client, patient_headers, admin_headers):
    """Scope enforcement is the whole point of access_type - a reports-only QR
    handed to a pharmacy must not reveal prescriptions."""
    grant = await client.post(
        "/api/patients/me/qr-access",
        json={"access_type": "reports_only", "duration_hours": 1},
        headers=patient_headers,
    )
    token = grant.json()["access_token"]

    await client.post(
        "/api/admin/pharmacies",
        json={
            "name": "Test Pharmacy",
            "license_number": "PHM-TEST-1",
            "address": "1 Test Road",
            "phone": "+911111111111",
            "email": "pharma@example.com",
            "password": "PharmaPass123",
        },
        headers=admin_headers,
    )
    login = await client.post(
        "/api/auth/login", json={"email": "pharma@example.com", "password": "PharmaPass123"}
    )
    pharmacy_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = await client.get(f"/api/pharmacy/prescriptions/qr/{token}", headers=pharmacy_headers)
    assert resp.status_code == 403


async def test_patient_cannot_read_another_patients_record_by_uhid(client, patient_headers):
    other = {
        "first_name": "Other",
        "last_name": "Person",
        "date_of_birth": "1990-01-01",
        "gender": "female",
        "phone": "+919888888888",
        "address": "Elsewhere",
        "email": "other.person@example.com",
        "password": "OtherPass123",
    }
    created = await client.post("/api/auth/register/patient", json=other)
    other_uhid = created.json()["uhid"]

    resp = await client.get(f"/api/patients/{other_uhid}", headers=patient_headers)
    assert resp.status_code == 403


async def test_pharmacy_endpoints_reject_a_patient_token(client, patient_headers):
    resp = await client.get("/api/pharmacy/dispensed-medicines", headers=patient_headers)
    assert resp.status_code == 403


@pytest_asyncio.fixture
async def prescription_setup(client, patient_headers, admin_headers, session_factory):
    """A patient with one active prescription, plus a logged-in pharmacy."""
    from sqlalchemy import select

    from src.core.security import hash_password
    from src.models.clinical import Medicine, Prescription, PrescriptionMedicine
    from src.models.enums import PrescriptionStatus, UserRole
    from src.models.organizations import Hospital
    from src.models.people import Doctor, Patient
    from src.models.user import User

    async with session_factory() as db:
        patient = (
            await db.execute(select(Patient).join(User).where(User.email == "test.patient@example.com"))
        ).scalar_one()

        hospital = Hospital(
            name="Rx Hospital",
            address="1 Rx Road",
            phone="+910000000000",
            email="rx@example.com",
            license_number="H-RX",
        )
        db.add(hospital)
        await db.flush()

        doc_user = User(
            email="dr.house@example.com",
            username="dr.house",
            full_name="Dr Gregory House",
            hashed_password=hash_password("DoctorPass123"),
            role=UserRole.DOCTOR,
        )
        db.add(doc_user)
        await db.flush()
        doctor = Doctor(
            user_id=doc_user.id,
            hospital_id=hospital.id,
            license_number="D-RX",
            specialization="General Medicine",
            years_of_experience=20,
            qualification="MD",
        )
        db.add(doctor)

        medicine = Medicine(name="Metformin", generic_name="Metformin HCl", strength="500mg", form="tablet")
        db.add(medicine)
        await db.flush()

        prescription = Prescription(
            patient_id=patient.id,
            doctor_id=doctor.id,
            status=PrescriptionStatus.ACTIVE,
            notes="After meals",
        )
        db.add(prescription)
        await db.flush()
        db.add(
            PrescriptionMedicine(
                prescription_id=prescription.id,
                medicine_id=medicine.id,
                dosage="1-0-1",
                frequency="Twice daily",
                duration="90 days",
                quantity_prescribed=180,
            )
        )
        await db.commit()
        prescription_id = prescription.id
        patient_uhid = patient.uhid

    grant = await client.post(
        "/api/patients/me/qr-access",
        json={"access_type": "prescription_only", "duration_hours": 1},
        headers=patient_headers,
    )
    token = grant.json()["access_token"]

    await client.post(
        "/api/admin/pharmacies",
        json={
            "name": "Dispense Pharmacy",
            "license_number": "PHM-RX-1",
            "address": "2 Rx Road",
            "phone": "+912222222222",
            "email": "rx.pharma@example.com",
            "password": "PharmaPass123",
        },
        headers=admin_headers,
    )
    login = await client.post(
        "/api/auth/login", json={"email": "rx.pharma@example.com", "password": "PharmaPass123"}
    )
    pharmacy_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    return {
        "token": token,
        "prescription_id": prescription_id,
        "patient_uhid": patient_uhid,
        "pharmacy_headers": pharmacy_headers,
    }


async def test_pharmacy_qr_lookup_carries_patient_and_doctor_names(client, prescription_setup):
    resp = await client.get(
        f"/api/pharmacy/prescriptions/qr/{prescription_setup['token']}",
        headers=prescription_setup["pharmacy_headers"],
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()[0]
    assert row["patient_name"] == "Test Patient"
    assert row["patient_uhid"] == prescription_setup["patient_uhid"]
    assert row["doctor_name"] == "Dr Gregory House"
    assert row["medicines"][0]["medicine_name"] == "Metformin"


async def test_pharmacy_dispense_flow(client, prescription_setup):
    pid = prescription_setup["prescription_id"]
    headers = prescription_setup["pharmacy_headers"]

    dispensed = await client.post(f"/api/pharmacy/dispense/{pid}", headers=headers)
    assert dispensed.status_code == 200, dispensed.text
    body = dispensed.json()
    assert body["status"] == "dispensed"
    assert body["patient_name"] == "Test Patient"

    history = await client.get("/api/pharmacy/dispensed-medicines", headers=headers)
    assert history.status_code == 200
    rows = history.json()
    assert len(rows) == 1
    assert rows[0]["patient_uhid"] == prescription_setup["patient_uhid"]
    assert rows[0]["doctor_name"] == "Dr Gregory House"

    again = await client.post(f"/api/pharmacy/dispense/{pid}", headers=headers)
    assert again.status_code == 409
    assert "already" in again.json()["detail"].lower()


async def test_admin_endpoints_reject_a_patient_token(client, patient_headers):
    resp = await client.get("/api/admin/hospitals", headers=patient_headers)
    assert resp.status_code == 403
