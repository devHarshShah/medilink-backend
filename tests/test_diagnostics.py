"""The lab loop: order a test, identify the patient by QR, publish results."""

from datetime import date

import pytest
import pytest_asyncio

from src.core.security import hash_password
from src.models.enums import Gender, QueuePriority, QueueStatus, QueueType, UserRole
from src.models.organizations import DiagnosticCenter, Hospital
from src.models.people import Doctor, Patient
from src.models.queue import QueueEntry
from src.models.user import User
from src.services import storage

_HASH = hash_password("Password123")
PDF = b"%PDF-1.7\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def stub_storage(monkeypatch):
    stored: dict[str, bytes] = {}
    monkeypatch.setattr(storage, "upload", lambda key, data, ct: stored.__setitem__(key, data))
    monkeypatch.setattr(
        storage, "presigned_url", lambda key, filename, expires_seconds=300: f"https://x/{key}"
    )
    return stored


@pytest_asyncio.fixture
async def lab_world(session_factory):
    async with session_factory() as db:
        hospital = Hospital(
            name="Lab Hospital",
            address="Road",
            phone="+910000000000",
            email="labh@example.com",
            license_number="H-LAB",
        )
        db.add(hospital)
        await db.flush()

        doc_user = User(
            email="lab.doc@example.com",
            username="lab.doc",
            full_name="Dr Lab",
            hashed_password=_HASH,
            role=UserRole.DOCTOR,
        )
        pat_user = User(
            email="lab.pat@example.com",
            username="lab.pat",
            full_name="Lab Patient",
            hashed_password=_HASH,
            role=UserRole.PATIENT,
        )
        centre_user = User(
            email="lab.centre@example.com",
            username="lab.centre",
            full_name="Lab Centre",
            hashed_password=_HASH,
            role=UserRole.DIAGNOSTIC_CENTER,
        )
        other_centre_user = User(
            email="other.centre@example.com",
            username="other.centre",
            full_name="Other Centre",
            hashed_password=_HASH,
            role=UserRole.DIAGNOSTIC_CENTER,
        )
        db.add_all([doc_user, pat_user, centre_user, other_centre_user])
        await db.flush()

        doctor = Doctor(
            user_id=doc_user.id,
            hospital_id=hospital.id,
            license_number="D-LAB",
            specialization="General Medicine",
            years_of_experience=5,
            qualification="MBBS",
        )
        patient = Patient(
            user_id=pat_user.id,
            uhid="ML2588888888",
            first_name="Lab",
            last_name="Patient",
            date_of_birth=date(1990, 1, 1),
            gender=Gender.MALE,
            phone="+919999999999",
            address="Somewhere",
        )
        centre = DiagnosticCenter(
            user_id=centre_user.id,
            name="Main Lab",
            license_number="DGC-MAIN",
            address="Lab Road",
            phone="+910000000001",
            email="lab.centre@example.com",
        )
        other_centre = DiagnosticCenter(
            user_id=other_centre_user.id,
            name="Other Lab",
            license_number="DGC-OTHER",
            address="Other Road",
            phone="+910000000002",
            email="other.centre@example.com",
        )
        db.add_all([doctor, patient, centre, other_centre])
        await db.flush()

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
            "patient_id": patient.id,
            "doctor_id": doctor.id,
            "centre_id": centre.id,
            "other_centre_id": other_centre.id,
            "queue_id": entry.id,
        }


async def _headers(client, email: str) -> dict[str, str]:
    login = await client.post("/api/auth/login", json={"email": email, "password": "Password123"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest_asyncio.fixture
async def dh(client, lab_world):
    return await _headers(client, "lab.doc@example.com")


@pytest_asyncio.fixture
async def lh(client, lab_world):
    return await _headers(client, "lab.centre@example.com")


@pytest_asyncio.fixture
async def ph(client, lab_world):
    return await _headers(client, "lab.pat@example.com")


async def _order(client, dh, lab_world, **overrides):
    body = {
        "patient_id": lab_world["patient_id"],
        "diagnostic_center_id": lab_world["centre_id"],
        "test_name": "Complete Blood Count",
        "test_type": "Pathology",
    }
    body.update(overrides)
    return await client.post("/api/doctor/lab-requests", json=body, headers=dh)


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------
async def test_doctor_orders_a_test(client, lab_world, dh):
    resp = await _order(client, dh, lab_world)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "requested"
    assert body["requested_by_doctor_name"] == "Dr Lab"


async def test_ordering_for_an_unknown_centre_is_refused(client, lab_world, dh):
    resp = await _order(client, dh, lab_world, diagnostic_center_id=99999)
    assert resp.status_code == 404


async def test_ordering_from_a_live_consultation(client, lab_world, dh):
    resp = await client.post(
        f"/api/doctor/consultation/{lab_world['queue_id']}/lab-request",
        json={
            "diagnostic_center_id": lab_world["centre_id"],
            "test_name": "Lipid Profile",
            "test_type": "Pathology",
        },
        headers=dh,
    )
    assert resp.status_code == 201
    assert resp.json()["patient_id"] == lab_world["patient_id"]


async def test_cannot_order_from_a_finished_consultation(client, lab_world, dh, session_factory):
    async with session_factory() as db:
        entry = await db.get(QueueEntry, lab_world["queue_id"])
        entry.status = QueueStatus.COMPLETED
        await db.commit()

    resp = await client.post(
        f"/api/doctor/consultation/{lab_world['queue_id']}/lab-request",
        json={
            "diagnostic_center_id": lab_world["centre_id"],
            "test_name": "X",
            "test_type": "Pathology",
        },
        headers=dh,
    )
    assert resp.status_code == 409


async def test_the_ordering_doctor_can_track_their_requests(client, lab_world, dh):
    await _order(client, dh, lab_world)
    body = (await client.get("/api/doctor/lab-requests", headers=dh)).json()
    assert len(body) == 1
    assert body[0]["test_name"] == "Complete Blood Count"


# --------------------------------------------------------------------------
# Centre portal
# --------------------------------------------------------------------------
async def test_centre_sees_only_its_own_requests(client, lab_world, dh, lh):
    await _order(client, dh, lab_world)
    await _order(client, dh, lab_world, diagnostic_center_id=lab_world["other_centre_id"])

    body = (await client.get("/api/diagnostics/requests", headers=lh)).json()
    assert len(body) == 1
    assert body[0]["diagnostic_center_id"] == lab_world["centre_id"]


async def test_centre_cannot_touch_another_centres_request(client, lab_world, dh, lh):
    report_id = (
        await _order(client, dh, lab_world, diagnostic_center_id=lab_world["other_centre_id"])
    ).json()["id"]
    resp = await client.patch(
        f"/api/diagnostics/requests/{report_id}/status",
        json={"new_status": "in_progress"},
        headers=lh,
    )
    assert resp.status_code == 404


async def test_status_moves_requested_to_in_progress(client, lab_world, dh, lh):
    report_id = (await _order(client, dh, lab_world)).json()["id"]
    resp = await client.patch(
        f"/api/diagnostics/requests/{report_id}/status",
        json={"new_status": "in_progress"},
        headers=lh,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"


async def test_a_completed_test_cannot_reopen(client, lab_world, dh, lh):
    """The doctor may already have acted on the result."""
    report_id = (await _order(client, dh, lab_world)).json()["id"]
    await client.put(
        f"/api/diagnostics/requests/{report_id}/results",
        json={"results": "Normal"},
        headers=lh,
    )
    resp = await client.patch(
        f"/api/diagnostics/requests/{report_id}/status",
        json={"new_status": "in_progress"},
        headers=lh,
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------
# Publishing results
# --------------------------------------------------------------------------
async def test_publishing_results_completes_the_test(client, lab_world, dh, lh):
    report_id = (await _order(client, dh, lab_world)).json()["id"]
    resp = await client.put(
        f"/api/diagnostics/requests/{report_id}/results",
        json={
            "results": "Hb 13.5 g/dL",
            "reference_ranges": "13.0 - 17.0 g/dL",
            "interpretation": "Within normal limits",
        },
        headers=lh,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["completed_date"] is not None


async def test_cannot_complete_a_test_with_no_findings(client, lab_world, dh, lh):
    """A completed test with nothing in it is indistinguishable from one nobody
    filled in."""
    report_id = (await _order(client, dh, lab_world)).json()["id"]
    resp = await client.put(
        f"/api/diagnostics/requests/{report_id}/results",
        json={"technician_notes": "Sample received"},
        headers=lh,
    )
    assert resp.status_code == 422


async def test_results_cannot_be_republished(client, lab_world, dh, lh):
    report_id = (await _order(client, dh, lab_world)).json()["id"]
    await client.put(f"/api/diagnostics/requests/{report_id}/results", json={"results": "Normal"}, headers=lh)
    again = await client.put(
        f"/api/diagnostics/requests/{report_id}/results", json={"results": "Actually abnormal"}, headers=lh
    )
    assert again.status_code == 409
    assert "fresh test" in again.json()["detail"].lower()


# --------------------------------------------------------------------------
# The loop closes
# --------------------------------------------------------------------------
async def test_results_reach_the_patient(client, lab_world, dh, lh, ph):
    report_id = (await _order(client, dh, lab_world)).json()["id"]
    await client.put(
        f"/api/diagnostics/requests/{report_id}/results",
        json={"results": "Hb 13.5 g/dL"},
        headers=lh,
    )

    reports = (await client.get("/api/patients/me/lab-reports", headers=ph)).json()
    assert len(reports) == 1
    assert reports[0]["results"] == "Hb 13.5 g/dL"
    assert reports[0]["status"] == "completed"


async def test_results_reach_the_ordering_doctor(client, lab_world, dh, lh):
    report_id = (await _order(client, dh, lab_world)).json()["id"]
    await client.put(
        f"/api/diagnostics/requests/{report_id}/results",
        json={"results": "Hb 13.5 g/dL"},
        headers=lh,
    )

    body = (await client.get("/api/doctor/lab-requests", headers=dh)).json()
    assert body[0]["results"] == "Hb 13.5 g/dL"


# --------------------------------------------------------------------------
# QR at the counter
# --------------------------------------------------------------------------
async def test_centre_identifies_a_patient_by_qr(client, lab_world, ph, lh):
    grant = await client.post(
        "/api/patients/me/qr-access",
        json={"access_type": "reports_only", "duration_hours": 1},
        headers=ph,
    )
    token = grant.json()["access_token"]

    resp = await client.get(f"/api/diagnostics/patient/qr/{token}", headers=lh)
    assert resp.status_code == 200
    assert resp.json()["uhid"] == "ML2588888888"


async def test_centre_sees_pending_tests_via_qr(client, lab_world, dh, ph, lh):
    await _order(client, dh, lab_world)
    grant = await client.post(
        "/api/patients/me/qr-access",
        json={"access_type": "reports_only", "duration_hours": 1},
        headers=ph,
    )
    token = grant.json()["access_token"]

    body = (await client.get(f"/api/diagnostics/requests/qr/{token}", headers=lh)).json()
    assert len(body) == 1
    assert body[0]["test_name"] == "Complete Blood Count"


async def test_a_prescription_only_qr_does_not_unlock_lab_requests(client, lab_world, ph, lh):
    grant = await client.post(
        "/api/patients/me/qr-access",
        json={"access_type": "prescription_only", "duration_hours": 1},
        headers=ph,
    )
    token = grant.json()["access_token"]
    resp = await client.get(f"/api/diagnostics/requests/qr/{token}", headers=lh)
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Attachments and stats
# --------------------------------------------------------------------------
async def test_attach_a_report_file(client, lab_world, dh, lh):
    report_id = (await _order(client, dh, lab_world)).json()["id"]
    resp = await client.post(
        f"/api/diagnostics/requests/{report_id}/attachment",
        files={"file": ("report.pdf", PDF, "application/pdf")},
        headers=lh,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_attachment"] is True


async def test_a_non_pdf_attachment_is_rejected_on_its_bytes(client, lab_world, dh, lh):
    report_id = (await _order(client, dh, lab_world)).json()["id"]
    resp = await client.post(
        f"/api/diagnostics/requests/{report_id}/attachment",
        files={"file": ("report.pdf", b"#!/bin/sh\n", "application/pdf")},
        headers=lh,
    )
    assert resp.status_code == 422


async def test_an_attachment_alone_can_complete_a_test(client, lab_world, dh, lh):
    """A scanned report is a result even without typed findings."""
    report_id = (await _order(client, dh, lab_world)).json()["id"]
    await client.post(
        f"/api/diagnostics/requests/{report_id}/attachment",
        files={"file": ("report.pdf", PDF, "application/pdf")},
        headers=lh,
    )
    resp = await client.put(
        f"/api/diagnostics/requests/{report_id}/results",
        json={"technician_notes": "See attached"},
        headers=lh,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


async def test_centre_stats_count_by_status(client, lab_world, dh, lh):
    await _order(client, dh, lab_world)
    second = (await _order(client, dh, lab_world, test_name="Lipid Profile")).json()["id"]
    await client.put(f"/api/diagnostics/requests/{second}/results", json={"results": "Normal"}, headers=lh)

    stats = (await client.get("/api/diagnostics/stats", headers=lh)).json()
    assert stats["pending_requests"] == 1
    assert stats["total_completed"] == 1
    assert stats["completed_today"] == 1


async def test_a_doctor_cannot_use_the_centre_portal(client, lab_world, dh):
    assert (await client.get("/api/diagnostics/requests", headers=dh)).status_code == 403
