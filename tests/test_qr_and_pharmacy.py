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


async def test_admin_endpoints_reject_a_patient_token(client, patient_headers):
    resp = await client.get("/api/admin/hospitals", headers=patient_headers)
    assert resp.status_code == 403
