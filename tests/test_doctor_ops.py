"""Doctor presence, schedules and stats."""

from datetime import date

import pytest_asyncio
from sqlalchemy import select

from src.core.security import hash_password
from src.models.doctor_ops import DoctorCheckIn
from src.models.enums import UserRole
from src.models.organizations import Hospital
from src.models.people import Doctor
from src.models.user import User

_HASH = hash_password("DoctorPass123")


@pytest_asyncio.fixture
async def world(session_factory):
    async with session_factory() as db:
        hospitals = []
        for name, license_no, lat, lng in (
            ("Alpha Hospital", "H-A", 12.9750, 77.6060),
            ("Beta Hospital", "H-B", 13.0358, 77.5970),
        ):
            hospital = Hospital(
                name=name,
                address=f"{name} Road",
                phone="+910000000000",
                email=f"{license_no.lower()}@example.com",
                license_number=license_no,
                latitude=lat,
                longitude=lng,
            )
            db.add(hospital)
            await db.flush()
            hospitals.append(hospital.id)

        user = User(
            email="ops.doc@example.com",
            username="ops.doc",
            full_name="Dr Ops",
            hashed_password=_HASH,
            role=UserRole.DOCTOR,
        )
        db.add(user)
        await db.flush()
        doctor = Doctor(
            user_id=user.id,
            hospital_id=hospitals[0],
            license_number="D-OPS",
            specialization="General Medicine",
            years_of_experience=8,
            qualification="MBBS",
            avg_consultation_minutes=10.0,
        )
        db.add(doctor)
        await db.commit()
        return {"alpha": hospitals[0], "beta": hospitals[1], "doctor": doctor.id}


@pytest_asyncio.fixture
async def dh(client, world):
    login = await client.post(
        "/api/auth/login", json={"email": "ops.doc@example.com", "password": "DoctorPass123"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# --------------------------------------------------------------------------
# Presence
# --------------------------------------------------------------------------
async def test_hospitals_use_the_prefixed_key_shape(client, world, dh):
    """doctor-checkin.tsx reads hospital_id/hospital_name, not id/name."""
    body = (await client.get("/api/doctor/hospitals", headers=dh)).json()
    assert len(body) == 2
    assert set(body[0]) >= {
        "hospital_id",
        "hospital_name",
        "hospital_address",
        "is_checked_in",
        "presence_status",
        "current_queue_size",
        "estimated_wait_time",
    }


async def test_doctor_starts_offline_and_not_checked_in(client, world, dh):
    body = (await client.get("/api/doctor/status", headers=dh)).json()
    assert body["is_checked_in"] is False
    assert body["presence_status"] == "offline"
    assert body["availabilities"] == []


async def test_check_in_makes_the_doctor_present(client, world, dh):
    resp = await client.post("/api/doctor/check-in", json={"hospital_id": world["alpha"]}, headers=dh)
    assert resp.status_code == 201, resp.text

    status = (await client.get("/api/doctor/status", headers=dh)).json()
    assert status["is_checked_in"] is True
    assert status["current_hospital"]["hospital_id"] == world["alpha"]
    assert len(status["availabilities"]) == 1


async def test_checking_in_twice_at_the_same_hospital_is_refused(client, world, dh):
    await client.post("/api/doctor/check-in", json={"hospital_id": world["alpha"]}, headers=dh)
    again = await client.post("/api/doctor/check-in", json={"hospital_id": world["alpha"]}, headers=dh)
    assert again.status_code == 409


async def test_checking_in_elsewhere_closes_the_forgotten_one(client, world, dh):
    """A doctor is only ever in one building. A stale open check-in would show
    them as simultaneously available in two places."""
    await client.post("/api/doctor/check-in", json={"hospital_id": world["alpha"]}, headers=dh)
    resp = await client.post("/api/doctor/check-in", json={"hospital_id": world["beta"]}, headers=dh)
    assert resp.status_code == 201
    assert resp.json()["auto_closed"], "the earlier check-in should have been closed"

    status = (await client.get("/api/doctor/status", headers=dh)).json()
    assert len(status["availabilities"]) == 1
    assert status["current_hospital"]["hospital_id"] == world["beta"]


async def test_check_out_requires_being_checked_in(client, world, dh):
    resp = await client.post("/api/doctor/check-out", json={}, headers=dh)
    assert resp.status_code == 409


async def test_check_out_records_the_next_hospital(client, world, dh):
    await client.post("/api/doctor/check-in", json={"hospital_id": world["alpha"]}, headers=dh)
    resp = await client.post(
        "/api/doctor/check-out",
        json={"notes": "Clinic finished", "next_hospital_id": world["beta"]},
        headers=dh,
    )
    assert resp.status_code == 200
    assert resp.json()["next_hospital_id"] == world["beta"]

    status = (await client.get("/api/doctor/status", headers=dh)).json()
    assert status["is_checked_in"] is False


async def test_presence_status_accepts_the_ui_synonym(client, world, dh):
    """The badge renders 'present'; rejecting it would 422 a value the UI shows."""
    await client.post("/api/doctor/check-in", json={"hospital_id": world["alpha"]}, headers=dh)
    resp = await client.put("/api/doctor/presence-status", json={"status": "present"}, headers=dh)
    assert resp.status_code == 200
    assert resp.json()["presence_status"] == "available"


async def test_on_break_maps_to_the_break_status(client, world, dh):
    await client.post("/api/doctor/check-in", json={"hospital_id": world["alpha"]}, headers=dh)
    resp = await client.put("/api/doctor/presence-status", json={"status": "on_break"}, headers=dh)
    assert resp.json()["presence_status"] == "break"


async def test_presence_status_requires_a_check_in_first(client, world, dh):
    resp = await client.put("/api/doctor/presence-status", json={"status": "busy"}, headers=dh)
    assert resp.status_code == 409


# --------------------------------------------------------------------------
# Availability feeds the queue
# --------------------------------------------------------------------------
async def test_a_checked_in_doctor_is_offered_at_that_hospital(client, world, dh, session_factory):
    from src.services.queue.engine import doctor_locations

    await client.post("/api/doctor/check-in", json={"hospital_id": world["beta"]}, headers=dh)

    async with session_factory() as db:
        rows = await doctor_locations(db, [world["doctor"]])
    # Checked in at Beta, so that is where they can be seen -- not their
    # primary hospital, Alpha.
    assert [h.id for _, h, _ in rows] == [world["beta"]]


async def test_a_doctor_on_break_attracts_no_new_patients(client, world, dh, session_factory):
    from src.services.queue.engine import doctor_locations

    await client.post("/api/doctor/check-in", json={"hospital_id": world["alpha"]}, headers=dh)
    await client.put("/api/doctor/presence-status", json={"status": "on_break"}, headers=dh)

    async with session_factory() as db:
        rows = await doctor_locations(db, [world["doctor"]])
    assert rows == [], "a doctor on a break is present but not taking new patients"


async def test_a_doctor_who_never_checked_in_falls_back_to_their_primary(client, world, session_factory):
    """The queue must work before anyone adopts check-in."""
    from src.services.queue.engine import doctor_locations

    async with session_factory() as db:
        rows = await doctor_locations(db, [world["doctor"]])
    assert [h.id for _, h, _ in rows] == [world["alpha"]]


async def test_a_checked_out_doctor_is_not_offered_anywhere(client, world, dh, session_factory):
    """Once they have used check-in, absence means absence -- no silent fallback
    to their primary hospital, which is the bug this feature exists to prevent."""
    from src.services.queue.engine import doctor_locations

    await client.post("/api/doctor/check-in", json={"hospital_id": world["alpha"]}, headers=dh)
    await client.post("/api/doctor/check-out", json={}, headers=dh)

    async with session_factory() as db:
        rows = await doctor_locations(db, [world["doctor"]])
    assert rows == []


# --------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------
def _schedule(world, **overrides):
    body = {
        "hospital_id": world["alpha"],
        "day_of_week": 0,
        "start_time": "09:00:00",
        "end_time": "13:00:00",
        "break_start_time": "11:00:00",
        "break_end_time": "11:30:00",
        "slot_duration": 15,
        "max_patients_per_slot": 2,
    }
    body.update(overrides)
    return body


async def test_create_and_list_a_schedule(client, world, dh):
    resp = await client.post("/api/doctor/schedules", json=_schedule(world), headers=dh)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["day_name"] == "Monday"
    assert body["start_time"] == "09:00"
    assert body["hospital_name"] == "Alpha Hospital"

    listed = (await client.get("/api/doctor/schedules", headers=dh)).json()
    assert len(listed) == 1


async def test_a_sitting_must_end_after_it_starts(client, world, dh):
    resp = await client.post(
        "/api/doctor/schedules",
        json=_schedule(world, start_time="13:00:00", end_time="09:00:00"),
        headers=dh,
    )
    assert resp.status_code == 422


async def test_a_break_must_fall_inside_the_sitting(client, world, dh):
    resp = await client.post(
        "/api/doctor/schedules",
        json=_schedule(world, break_start_time="18:00:00", break_end_time="18:30:00"),
        headers=dh,
    )
    assert resp.status_code == 422


async def test_a_half_specified_break_is_rejected(client, world, dh):
    resp = await client.post(
        "/api/doctor/schedules",
        json=_schedule(world, break_end_time=None),
        headers=dh,
    )
    assert resp.status_code == 422


async def test_overlapping_sittings_are_refused(client, world, dh):
    """A doctor cannot be in two hospitals at once, so publishing both would
    advertise availability that cannot be honoured."""
    await client.post("/api/doctor/schedules", json=_schedule(world), headers=dh)
    clash = await client.post(
        "/api/doctor/schedules",
        json=_schedule(
            world,
            hospital_id=world["beta"],
            start_time="12:00:00",
            end_time="16:00:00",
            break_start_time=None,
            break_end_time=None,
        ),
        headers=dh,
    )
    assert clash.status_code == 409
    assert "overlaps" in clash.json()["detail"].lower()


async def test_a_non_overlapping_second_sitting_is_allowed(client, world, dh):
    await client.post("/api/doctor/schedules", json=_schedule(world), headers=dh)
    later = await client.post(
        "/api/doctor/schedules",
        json=_schedule(
            world,
            hospital_id=world["beta"],
            start_time="14:00:00",
            end_time="18:00:00",
            break_start_time=None,
            break_end_time=None,
        ),
        headers=dh,
    )
    assert later.status_code == 201


async def test_a_doctor_cannot_delete_someone_elses_schedule(client, world, dh):
    resp = await client.delete("/api/doctor/schedules/99999", headers=dh)
    assert resp.status_code == 404


async def test_todays_schedule_appears_on_the_hospital_list(client, world, dh):
    await client.post(
        "/api/doctor/schedules",
        json=_schedule(world, day_of_week=date.today().weekday()),
        headers=dh,
    )
    body = (await client.get("/api/doctor/hospitals", headers=dh)).json()
    alpha = next(h for h in body if h["hospital_id"] == world["alpha"])
    assert alpha["schedule_today"] is not None
    assert alpha["schedule_today"]["start_time"] == "09:00"


# --------------------------------------------------------------------------
# Leaves
# --------------------------------------------------------------------------
async def test_create_and_list_a_leave(client, world, dh):
    resp = await client.post(
        "/api/doctor/leaves",
        json={
            "leave_type": "vacation",
            "start_date": "2026-09-01",
            "end_date": "2026-09-07",
            "reason": "Annual leave",
        },
        headers=dh,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_approved"] is False

    listed = (await client.get("/api/doctor/leaves", headers=dh)).json()
    assert len(listed) == 1
    assert listed[0]["leave_type"] == "vacation"


async def test_leave_cannot_end_before_it_starts(client, world, dh):
    resp = await client.post(
        "/api/doctor/leaves",
        json={"leave_type": "sick", "start_date": "2026-09-07", "end_date": "2026-09-01"},
        headers=dh,
    )
    assert resp.status_code == 422


async def test_part_day_leave_needs_both_times(client, world, dh):
    resp = await client.post(
        "/api/doctor/leaves",
        json={
            "leave_type": "conference",
            "start_date": "2026-09-01",
            "end_date": "2026-09-01",
            "start_time": "09:00:00",
        },
        headers=dh,
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------
async def test_doctor_stats_return_real_zeroes_not_mocks(client, world, dh):
    """The dashboard previously hardcoded 8/5/12. An empty database must
    report zero, not invented activity."""
    body = (await client.get("/api/doctor/stats", headers=dh)).json()
    assert body["today_consultations"] == 0
    assert body["current_queue_size"] == 0
    assert body["active_prescriptions"] == 0
    assert body["pending_reports"] == 0
    assert body["avg_consultation_time"] == 10.0


async def test_doctor_stats_do_not_invent_a_satisfaction_score(client, world, dh):
    """There is no feedback mechanism, so this must be null rather than fabricated."""
    body = (await client.get("/api/doctor/stats", headers=dh)).json()
    assert body["patient_satisfaction"] is None


async def test_stats_require_a_doctor(client, world, patient_payload):
    await client.post("/api/auth/register/patient", json=patient_payload)
    login = await client.post(
        "/api/auth/login",
        json={"email": patient_payload["email"], "password": patient_payload["password"]},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/doctor/stats", headers=headers)).status_code == 403


async def test_pharmacy_stats_start_at_zero(client, admin_headers):
    await client.post(
        "/api/admin/pharmacies",
        json={
            "name": "Stats Pharmacy",
            "license_number": "PHM-STATS",
            "address": "1 Road",
            "phone": "+911111111111",
            "email": "stats.pharmacy@example.com",
            "password": "PharmaPass123",
        },
        headers=admin_headers,
    )
    login = await client.post(
        "/api/auth/login",
        json={"email": "stats.pharmacy@example.com", "password": "PharmaPass123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    body = (await client.get("/api/pharmacy/stats", headers=headers)).json()
    assert body["total_prescriptions"] == 0
    assert body["today_dispensed"] == 0
    assert body["total_revenue"] == 0.0


async def test_check_in_is_recorded_in_the_database(client, world, dh, session_factory):
    await client.post("/api/doctor/check-in", json={"hospital_id": world["alpha"]}, headers=dh)
    async with session_factory() as db:
        rows = (await db.execute(select(DoctorCheckIn))).scalars().all()
    assert len(rows) == 1
    assert rows[0].check_out_time is None
