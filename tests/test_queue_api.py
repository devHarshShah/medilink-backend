"""Queue integration tests.

Cover the contract the frontend depends on and the behaviours that make the
cross-hospital idea work: assignment, floating re-assignment, lock-in, priority
ordering, and status transitions.
"""

import pytest_asyncio

from src.models.enums import QueueStatus, QueueType
from src.models.organizations import Hospital
from src.models.people import Doctor, Patient
from src.models.queue import QueueEntry
from src.models.user import User

# Two points ~5km apart; a third far away.
NEAR = (12.9750, 77.6060)
MID = (12.9352, 77.6245)
FAR = (13.0358, 77.5970)


@pytest_asyncio.fixture
async def world(session_factory, client):
    """Two hospitals, two doctors, one patient."""
    from src.core.security import hash_password
    from src.models.enums import UserRole

    async with session_factory() as db:
        near_hospital = Hospital(
            name="Near Hospital",
            address="MG Road",
            phone="+910000000001",
            email="near@example.com",
            license_number="H-NEAR",
            latitude=NEAR[0],
            longitude=NEAR[1],
        )
        far_hospital = Hospital(
            name="Far Hospital",
            address="Hebbal",
            phone="+910000000002",
            email="far@example.com",
            license_number="H-FAR",
            latitude=FAR[0],
            longitude=FAR[1],
        )
        db.add_all([near_hospital, far_hospital])
        await db.flush()

        doctors = {}
        for key, hospital, license_no in (
            ("near", near_hospital, "D-NEAR"),
            ("far", far_hospital, "D-FAR"),
        ):
            user = User(
                email=f"doc.{key}@example.com",
                username=f"doc.{key}",
                full_name=f"Dr {key.title()}",
                hashed_password=hash_password("DoctorPass123"),
                role=UserRole.DOCTOR,
            )
            db.add(user)
            await db.flush()
            doctor = Doctor(
                user_id=user.id,
                hospital_id=hospital.id,
                license_number=license_no,
                specialization="General Medicine",
                years_of_experience=10,
                qualification="MBBS",
                avg_consultation_minutes=10.0,
            )
            db.add(doctor)
            await db.flush()
            doctors[key] = doctor.id

        await db.commit()
        return {
            "near_hospital": near_hospital.id,
            "far_hospital": far_hospital.id,
            "near_doctor": doctors["near"],
            "far_doctor": doctors["far"],
        }


@pytest_asyncio.fixture
async def patient_headers(client, patient_payload):
    await client.post("/api/auth/register/patient", json=patient_payload)
    login = await client.post(
        "/api/auth/login",
        json={"email": patient_payload["email"], "password": patient_payload["password"]},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest_asyncio.fixture
async def doctor_headers(client, world):
    login = await client.post(
        "/api/auth/login", json={"email": "doc.near@example.com", "password": "DoctorPass123"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _join_body(world, **overrides):
    body = {
        "doctor_ids": [world["near_doctor"], world["far_doctor"]],
        "symptoms_description": "Fever and body ache for two days",
        "queue_type": "cross_hospital",
        "patient_location": {"latitude": NEAR[0], "longitude": NEAR[1]},
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------
# Joining
# --------------------------------------------------------------------------
async def test_join_assigns_the_nearest_centre_when_queues_are_equal(client, world, patient_headers):
    resp = await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["assigned_hospital_id"] == world["near_hospital"]
    assert body["queue_number"] == 1
    assert body["current_position"] == 1


async def test_join_returns_the_runners_up_as_alternatives(client, world, patient_headers):
    body = (await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)).json()
    assert body["alternative_options"], "the patient should see what else was available"
    assert body["alternative_options"][0]["doctor_id"] == world["far_doctor"]


async def test_join_returns_a_leave_at_time(client, world, patient_headers):
    """The whole product idea: wait at home, leave when told."""
    body = (await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)).json()
    assert body["leave_at"] is not None
    assert body["travel_time_minutes"] is not None


async def test_join_without_location_still_works(client, world, patient_headers):
    """Geolocation is optional in the browser."""
    resp = await client.post(
        "/api/queue/join", json=_join_body(world, patient_location=None), headers=patient_headers
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


async def test_cannot_hold_two_active_queue_entries(client, world, patient_headers):
    """Otherwise one patient inflates every queue they touch."""
    await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)
    second = await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)
    body = second.json()
    assert body["success"] is False
    assert "already in a queue" in body["error"].lower()


async def test_join_with_unknown_doctor_fails_softly(client, world, patient_headers):
    """The wizard renders `error` inline; it must not be an HTTP error."""
    resp = await client.post(
        "/api/queue/join", json=_join_body(world, doctor_ids=[9999]), headers=patient_headers
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert resp.json()["error"]


async def test_emergency_triage_overrides_a_client_supplied_priority(
    client, world, patient_headers, session_factory
):
    """A client posting priority 'normal' for a stroke must not be believed."""
    resp = await client.post(
        "/api/queue/join",
        json=_join_body(
            world,
            priority="normal",
            ai_recommendation={"urgency_level": "emergency", "summary": "Seek care now"},
        ),
        headers=patient_headers,
    )
    queue_id = resp.json()["queue_id"]

    async with session_factory() as db:
        entry = await db.get(QueueEntry, queue_id)
        assert entry.priority.value == "emergency"
        assert entry.is_emergency is True


async def _add_doctor(session_factory, hospital_id, license_no, specialization, avg=10.0):
    from src.core.security import hash_password
    from src.models.enums import UserRole

    async with session_factory() as db:
        user = User(
            email=f"{license_no.lower()}@example.com",
            username=license_no.lower(),
            full_name=f"Dr {license_no}",
            hashed_password=hash_password("DoctorPass123"),
            role=UserRole.DOCTOR,
        )
        db.add(user)
        await db.flush()
        doctor = Doctor(
            user_id=user.id,
            hospital_id=hospital_id,
            license_number=license_no,
            specialization=specialization,
            years_of_experience=10,
            qualification="MBBS, MD",
            avg_consultation_minutes=avg,
        )
        db.add(doctor)
        await db.commit()
        return doctor.id


def _cardiology_high():
    return {
        "urgency_level": "high",
        "recommended_specializations": [
            {"specialization": "Cardiology", "priority": 1, "reason": "chest pain"},
        ],
    }


async def test_high_urgency_join_is_routed_to_a_specialist_not_the_chosen_generalist(
    client, world, patient_headers, session_factory
):
    """The patient only picked a General Medicine doctor, but triage flags a
    high-urgency cardiac case -- assignment must widen to a Cardiologist."""
    cardio_id = await _add_doctor(session_factory, world["far_hospital"], "D-CARD", "Cardiology")

    resp = await client.post(
        "/api/queue/join",
        json=_join_body(
            world,
            doctor_ids=[world["near_doctor"]],  # generalist only
            ai_recommendation=_cardiology_high(),
        ),
        headers=patient_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["doctor_id"] == cardio_id
    assert body["doctor_specialization"] == "Cardiology"


async def test_low_urgency_join_keeps_the_chosen_generalist(
    client, world, patient_headers, session_factory
):
    """Same shape, but non-urgent: the soft match penalty applies and the
    faster generalist the patient picked is kept."""
    await _add_doctor(session_factory, world["far_hospital"], "D-CARD2", "Cardiology")

    resp = await client.post(
        "/api/queue/join",
        json=_join_body(
            world,
            doctor_ids=[world["near_doctor"]],
            ai_recommendation={
                "urgency_level": "low",
                "recommended_specializations": [
                    {"specialization": "Cardiology", "priority": 1, "reason": "x"}
                ],
            },
        ),
        headers=patient_headers,
    )
    body = resp.json()
    assert body["success"] is True
    assert body["doctor_id"] == world["near_doctor"]


async def test_high_urgency_join_fails_softly_when_no_specialist_exists(
    client, world, patient_headers
):
    resp = await client.post(
        "/api/queue/join",
        json=_join_body(
            world, doctor_ids=[world["near_doctor"]], ai_recommendation=_cardiology_high()
        ),
        headers=patient_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "cardiology" in body["error"].lower()
    assert "emergency" in body["error"].lower()


async def test_hospital_specific_join_pins_to_that_hospital(client, world, patient_headers):
    resp = await client.post(
        "/api/queue/join",
        json=_join_body(world, queue_type="hospital_specific", hospital_id=world["far_hospital"]),
        headers=patient_headers,
    )
    assert resp.json()["assigned_hospital_id"] == world["far_hospital"]


# --------------------------------------------------------------------------
# Status and listing
# --------------------------------------------------------------------------
async def test_my_queues_uses_an_envelope(client, world, patient_headers):
    """my-queues/page.tsx reads `response.queues`, not a bare array."""
    await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)
    body = (await client.get("/api/queue/my-queues", headers=patient_headers)).json()
    assert isinstance(body, dict)
    assert isinstance(body["queues"], list)
    assert len(body["queues"]) == 1


async def test_status_serves_both_hospital_field_spellings(client, world, patient_headers):
    """Different screens read hospital_name vs assigned_hospital_name."""
    queue_id = (await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)).json()[
        "queue_id"
    ]
    body = (await client.get(f"/api/queue/status/{queue_id}", headers=patient_headers)).json()
    assert body["hospital_name"] == body["assigned_hospital_name"]
    assert body["hospital_id"] == body["assigned_hospital_id"]


async def test_patient_cannot_read_another_patients_queue(client, world, patient_headers):
    queue_id = (await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)).json()[
        "queue_id"
    ]

    other = {
        "first_name": "Other",
        "last_name": "Person",
        "date_of_birth": "1990-01-01",
        "gender": "female",
        "phone": "+919888888888",
        "address": "Elsewhere",
        "email": "other.queue@example.com",
        "password": "OtherPass123",
    }
    await client.post("/api/auth/register/patient", json=other)
    login = await client.post(
        "/api/auth/login", json={"email": other["email"], "password": other["password"]}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = await client.get(f"/api/queue/status/{queue_id}", headers=headers)
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Positions and priority
# --------------------------------------------------------------------------
async def test_emergency_jumps_ahead_of_waiting_patients(client, world, session_factory):
    """Position must reflect the order patients are actually called."""
    from datetime import date

    from src.core.security import hash_password
    from src.models.enums import Gender, QueuePriority, UserRole

    async with session_factory() as db:
        entries = []
        for index, priority in enumerate(
            [QueuePriority.NORMAL, QueuePriority.NORMAL, QueuePriority.EMERGENCY]
        ):
            user = User(
                email=f"p{index}@example.com",
                username=f"p{index}",
                full_name=f"P{index}",
                hashed_password=hash_password("PatientPass123"),
                role=UserRole.PATIENT,
            )
            db.add(user)
            await db.flush()
            patient = Patient(
                user_id=user.id,
                uhid=f"ML250000010{index}",
                first_name=f"P{index}",
                last_name="Test",
                date_of_birth=date(1990, 1, 1),
                gender=Gender.MALE,
                phone="+910000000000",
                address="Test",
            )
            db.add(patient)
            await db.flush()
            entry = QueueEntry(
                patient_id=patient.id,
                doctor_id=world["near_doctor"],
                hospital_id=world["near_hospital"],
                queue_type=QueueType.CROSS_HOSPITAL,
                queue_number=index + 1,
                status=QueueStatus.WAITING,
                priority=priority,
                is_emergency=priority is QueuePriority.EMERGENCY,
            )
            db.add(entry)
            await db.flush()
            entries.append(entry.id)
        await db.commit()

    from src.services.queue.engine import position_in_queue

    async with session_factory() as db:
        emergency = await db.get(QueueEntry, entries[2])
        first_normal = await db.get(QueueEntry, entries[0])
        assert await position_in_queue(db, emergency) == 1
        assert await position_in_queue(db, first_normal) == 2


# --------------------------------------------------------------------------
# Doctor view and transitions
# --------------------------------------------------------------------------
async def test_doctor_sees_their_queue(client, world, patient_headers, doctor_headers):
    await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)
    body = (await client.get(f"/api/queue/doctor/{world['near_doctor']}", headers=doctor_headers)).json()
    assert body["total_waiting"] == 1
    assert body["queue"][0]["patient_uhid"].startswith("ML")


async def test_doctor_cannot_read_another_doctors_queue(client, world, doctor_headers):
    resp = await client.get(f"/api/queue/doctor/{world['far_doctor']}", headers=doctor_headers)
    assert resp.status_code == 403


async def test_doctor_can_start_and_complete_a_consultation(client, world, patient_headers, doctor_headers):
    queue_id = (await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)).json()[
        "queue_id"
    ]

    started = await client.patch(
        f"/api/queue/status/{queue_id}",
        json={"new_status": "in_consultation"},
        headers=doctor_headers,
    )
    assert started.status_code == 200

    completed = await client.patch(
        f"/api/queue/status/{queue_id}", json={"new_status": "completed"}, headers=doctor_headers
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"


async def test_illegal_status_transition_is_rejected(client, world, patient_headers, doctor_headers):
    """A completed entry jumping back to waiting would corrupt every position
    calculation behind it."""
    queue_id = (await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)).json()[
        "queue_id"
    ]

    await client.patch(
        f"/api/queue/status/{queue_id}",
        json={"new_status": "in_consultation"},
        headers=doctor_headers,
    )
    await client.patch(
        f"/api/queue/status/{queue_id}", json={"new_status": "completed"}, headers=doctor_headers
    )
    resp = await client.patch(
        f"/api/queue/status/{queue_id}", json={"new_status": "waiting"}, headers=doctor_headers
    )
    assert resp.status_code == 409


async def test_completing_a_consultation_updates_the_doctors_average(
    client, world, patient_headers, doctor_headers, session_factory
):
    from datetime import UTC, datetime, timedelta

    queue_id = (await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)).json()[
        "queue_id"
    ]
    await client.patch(
        f"/api/queue/status/{queue_id}",
        json={"new_status": "in_consultation"},
        headers=doctor_headers,
    )

    # Backdate the start so the consultation reads as 20 minutes long.
    async with session_factory() as db:
        entry = await db.get(QueueEntry, queue_id)
        entry.consultation_started_at = datetime.now(UTC) - timedelta(minutes=20)
        await db.commit()

    await client.patch(
        f"/api/queue/status/{queue_id}", json={"new_status": "completed"}, headers=doctor_headers
    )

    async with session_factory() as db:
        doctor = await db.get(Doctor, world["near_doctor"])
        # EMA from 10.0 toward 20.0 -> 12.0
        assert doctor.avg_consultation_minutes > 10.0


async def test_absurd_consultation_duration_is_ignored(
    client, world, patient_headers, doctor_headers, session_factory
):
    """A forgotten 'complete' must not teach the model that consultations take
    eight hours and wreck every wait estimate."""
    from datetime import UTC, datetime, timedelta

    queue_id = (await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)).json()[
        "queue_id"
    ]
    await client.patch(
        f"/api/queue/status/{queue_id}",
        json={"new_status": "in_consultation"},
        headers=doctor_headers,
    )
    async with session_factory() as db:
        entry = await db.get(QueueEntry, queue_id)
        entry.consultation_started_at = datetime.now(UTC) - timedelta(hours=8)
        await db.commit()

    await client.patch(
        f"/api/queue/status/{queue_id}", json={"new_status": "completed"}, headers=doctor_headers
    )

    async with session_factory() as db:
        doctor = await db.get(Doctor, world["near_doctor"])
        assert doctor.avg_consultation_minutes == 10.0


# --------------------------------------------------------------------------
# Cancelling
# --------------------------------------------------------------------------
async def test_patient_can_cancel_and_then_rejoin(client, world, patient_headers):
    queue_id = (await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)).json()[
        "queue_id"
    ]

    cancelled = await client.delete(f"/api/queue/cancel/{queue_id}", headers=patient_headers)
    assert cancelled.status_code == 200

    again = await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)
    assert again.json()["success"] is True


async def test_cannot_cancel_once_the_consultation_started(client, world, patient_headers, doctor_headers):
    queue_id = (await client.post("/api/queue/join", json=_join_body(world), headers=patient_headers)).json()[
        "queue_id"
    ]
    await client.patch(
        f"/api/queue/status/{queue_id}",
        json={"new_status": "in_consultation"},
        headers=doctor_headers,
    )
    resp = await client.delete(f"/api/queue/cancel/{queue_id}", headers=patient_headers)
    assert resp.status_code == 409


# --------------------------------------------------------------------------
# Available doctors
# --------------------------------------------------------------------------
async def test_available_doctors_accepts_comma_joined_specializations(client, world, patient_headers):
    """The client joins the list into one query param rather than repeating it."""
    resp = await client.get(
        "/api/queue/available-doctors",
        params={
            "queue_type": "cross_hospital",
            "specializations": "General Medicine,Cardiology",
            "include_general": "true",
        },
        headers=patient_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_available"] == 2
    assert {d["specialization"] for d in body["doctors"]} == {"General Medicine"}


async def test_available_doctors_ranks_by_distance_when_given_coordinates(client, world, patient_headers):
    resp = await client.get(
        "/api/queue/available-doctors",
        params={
            "queue_type": "cross_hospital",
            "include_general": "true",
            "latitude": NEAR[0],
            "longitude": NEAR[1],
        },
        headers=patient_headers,
    )
    doctors = resp.json()["doctors"]
    assert doctors[0]["doctor_id"] == world["near_doctor"]
    assert doctors[0]["distance"] is not None


async def test_queue_hospitals_lists_active_hospitals(client, world, patient_headers):
    resp = await client.get("/api/queue/hospitals", headers=patient_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2
