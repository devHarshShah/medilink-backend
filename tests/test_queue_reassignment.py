"""Floating re-assignment and lock-in.

The plan's headline behaviour: a waiting patient is moved to a better centre
while they are still at home, and stops being moved once they should be
travelling.
"""

from datetime import UTC, date, datetime, timedelta

import pytest_asyncio

from src.core.security import hash_password
from src.models.enums import Gender, QueuePriority, QueueStatus, QueueType, UserRole
from src.models.organizations import Hospital
from src.models.people import Doctor, Patient
from src.models.queue import QueueEntry
from src.models.user import User
from src.services.queue.engine import rescore_all_waiting, rescore_entry

NEAR = (12.9750, 77.6060)
FAR = (13.0358, 77.5970)

# Hash once. These fixtures create dozens of filler patients and bcrypt is
# deliberately slow, so hashing per row dominated the suite's runtime.
_PASSWORD_HASH = hash_password("PatientPass123")


async def _make_hospital(db, name, license_no, coords) -> Hospital:
    hospital = Hospital(
        name=name,
        address=name,
        phone="+910000000000",
        email=f"{license_no.lower()}@example.com",
        license_number=license_no,
        latitude=coords[0],
        longitude=coords[1],
    )
    db.add(hospital)
    await db.flush()
    return hospital


async def _make_doctor(db, hospital, license_no, avg=10.0, specialization="General Medicine") -> Doctor:
    user = User(
        email=f"{license_no.lower()}@example.com",
        username=license_no.lower(),
        full_name=f"Dr {license_no}",
        hashed_password=_PASSWORD_HASH,
        role=UserRole.DOCTOR,
    )
    db.add(user)
    await db.flush()
    doctor = Doctor(
        user_id=user.id,
        hospital_id=hospital.id,
        license_number=license_no,
        specialization=specialization,
        years_of_experience=5,
        qualification="MBBS",
        avg_consultation_minutes=avg,
    )
    db.add(doctor)
    await db.flush()
    return doctor


async def _make_patient(db, suffix: str) -> Patient:
    user = User(
        email=f"pt{suffix}@example.com",
        username=f"pt{suffix}",
        full_name=f"Patient {suffix}",
        hashed_password=_PASSWORD_HASH,
        role=UserRole.PATIENT,
    )
    db.add(user)
    await db.flush()
    patient = Patient(
        user_id=user.id,
        uhid=f"ML25{suffix:0>10}",
        first_name="Patient",
        last_name=suffix,
        date_of_birth=date(1990, 1, 1),
        gender=Gender.MALE,
        phone="+910000000000",
        address="Test",
    )
    db.add(patient)
    await db.flush()
    return patient


async def _fill_queue(db, doctor, hospital, count: int) -> None:
    """Put `count` other patients ahead in this doctor's queue."""
    for i in range(count):
        other = await _make_patient(db, f"f{doctor.id}{hospital.id}{i}")
        db.add(
            QueueEntry(
                patient_id=other.id,
                doctor_id=doctor.id,
                hospital_id=hospital.id,
                queue_type=QueueType.CROSS_HOSPITAL,
                queue_number=i + 1,
                status=QueueStatus.WAITING,
                priority=QueuePriority.NORMAL,
            )
        )
    await db.flush()


@pytest_asyncio.fixture
async def scenario(session_factory):
    """Patient sits at NEAR, assigned to a badly congested near hospital while
    a quiet far hospital exists."""
    async with session_factory() as db:
        near_hospital = await _make_hospital(db, "Near Hospital", "H-NEAR", NEAR)
        far_hospital = await _make_hospital(db, "Far Hospital", "H-FAR", FAR)
        near_doctor = await _make_doctor(db, near_hospital, "D-NEAR")
        far_doctor = await _make_doctor(db, far_hospital, "D-FAR")

        # 40 people ahead at the near hospital: ~400 minutes.
        await _fill_queue(db, near_doctor, near_hospital, 40)

        patient = await _make_patient(db, "main")
        entry = QueueEntry(
            patient_id=patient.id,
            doctor_id=near_doctor.id,
            hospital_id=near_hospital.id,
            queue_type=QueueType.CROSS_HOSPITAL,
            queue_number=41,
            status=QueueStatus.WAITING,
            priority=QueuePriority.NORMAL,
            candidate_doctor_ids=[near_doctor.id, far_doctor.id],
            patient_latitude=NEAR[0],
            patient_longitude=NEAR[1],
        )
        db.add(entry)
        await db.commit()

        return {
            "entry_id": entry.id,
            "near_doctor": near_doctor.id,
            "far_doctor": far_doctor.id,
            "near_hospital": near_hospital.id,
            "far_hospital": far_hospital.id,
        }


async def test_patient_is_moved_to_a_faster_centre(session_factory, scenario):
    """400 minutes of queue nearby vs an empty queue further out -- move."""
    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        moved = await rescore_entry(db, entry)
        assert moved is True

    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        assert entry.doctor_id == scenario["far_doctor"]
        assert entry.hospital_id == scenario["far_hospital"]
        assert entry.reassignment_count == 1


async def test_a_moved_patient_gets_a_new_token_number(session_factory, scenario):
    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        await rescore_entry(db, entry)

    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        # Far hospital was empty, so this is token 1 there -- not the 41 it
        # held at the congested hospital.
        assert entry.queue_number == 1


async def test_marginal_difference_does_not_move_the_patient(session_factory):
    """Hysteresis: without it a patient oscillates on every scoring pass."""
    async with session_factory() as db:
        h1 = await _make_hospital(db, "A", "H-A", NEAR)
        h2 = await _make_hospital(db, "B", "H-B", NEAR)
        # Short consultations, so one extra person ahead is only a few minutes
        # -- below the hysteresis margin. (Same coordinates, so moving is
        # otherwise free and even a small gain would justify it.)
        d1 = await _make_doctor(db, h1, "D-A", avg=4.0)
        d2 = await _make_doctor(db, h2, "D-B", avg=4.0)
        await _fill_queue(db, d1, h1, 2)
        await _fill_queue(db, d2, h2, 1)

        patient = await _make_patient(db, "marginal")
        entry = QueueEntry(
            patient_id=patient.id,
            doctor_id=d1.id,
            hospital_id=h1.id,
            queue_type=QueueType.CROSS_HOSPITAL,
            queue_number=4,
            status=QueueStatus.WAITING,
            priority=QueuePriority.NORMAL,
            candidate_doctor_ids=[d1.id, d2.id],
            patient_latitude=NEAR[0],
            patient_longitude=NEAR[1],
        )
        db.add(entry)
        await db.commit()
        entry_id = entry.id

    async with session_factory() as db:
        entry = await db.get(QueueEntry, entry_id)
        assert await rescore_entry(db, entry) is False


async def test_a_locked_in_patient_is_never_moved(session_factory, scenario):
    """Being re-routed mid-journey is worse than a longer wait."""
    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        entry.locked_in_at = datetime.now(UTC)
        await db.commit()

    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        assert await rescore_entry(db, entry) is False
        assert entry.doctor_id == scenario["near_doctor"]


async def test_an_emergency_is_never_moved(session_factory, scenario):
    """Continuity of care beats optimisation."""
    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        entry.priority = QueuePriority.EMERGENCY
        entry.is_emergency = True
        await db.commit()

    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        assert await rescore_entry(db, entry) is False


async def test_a_hospital_specific_entry_is_never_moved(session_factory, scenario):
    """Choosing one hospital is a promise the server must keep."""
    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        entry.queue_type = QueueType.HOSPITAL_SPECIFIC
        await db.commit()

    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        assert await rescore_entry(db, entry) is False


async def test_a_patient_close_to_their_turn_locks_in_instead_of_moving(session_factory):
    """The lock-in boundary: once wait <= travel + buffer, freeze the slot."""
    async with session_factory() as db:
        near_hospital = await _make_hospital(db, "Near", "H-N2", NEAR)
        far_hospital = await _make_hospital(db, "Far", "H-F2", FAR)
        near_doctor = await _make_doctor(db, near_hospital, "D-N2")
        far_doctor = await _make_doctor(db, far_hospital, "D-F2")

        # Only one person ahead: their turn is imminent.
        await _fill_queue(db, near_doctor, near_hospital, 1)

        patient = await _make_patient(db, "lockin")
        entry = QueueEntry(
            patient_id=patient.id,
            doctor_id=near_doctor.id,
            hospital_id=near_hospital.id,
            queue_type=QueueType.CROSS_HOSPITAL,
            queue_number=2,
            status=QueueStatus.WAITING,
            priority=QueuePriority.NORMAL,
            candidate_doctor_ids=[near_doctor.id, far_doctor.id],
            patient_latitude=NEAR[0],
            patient_longitude=NEAR[1],
        )
        db.add(entry)
        await db.commit()
        entry_id = entry.id

    async with session_factory() as db:
        entry = await db.get(QueueEntry, entry_id)
        moved = await rescore_entry(db, entry)
        assert moved is False

    async with session_factory() as db:
        entry = await db.get(QueueEntry, entry_id)
        assert entry.locked_in_at is not None, "an imminent turn must lock the slot"
        assert entry.doctor_id == near_doctor.id


async def test_rescoring_updates_leave_at(session_factory, scenario):
    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        await rescore_entry(db, entry)

    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        assert entry.leave_at is not None
        leave_at = entry.leave_at
        if leave_at.tzinfo is None:
            leave_at = leave_at.replace(tzinfo=UTC)
        assert leave_at >= datetime.now(UTC) - timedelta(seconds=5)


async def test_sweep_only_touches_waiting_cross_hospital_entries(session_factory, scenario):
    async with session_factory() as db:
        moved = await rescore_all_waiting(db)
        assert moved == 1

    # A second pass finds no further improvement -- the sweep is idempotent.
    async with session_factory() as db:
        assert await rescore_all_waiting(db) == 0


async def test_completed_entries_are_not_rescored(session_factory, scenario):
    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        entry.status = QueueStatus.COMPLETED
        await db.commit()

    async with session_factory() as db:
        entry = await db.get(QueueEntry, scenario["entry_id"])
        assert await rescore_entry(db, entry) is False


# --------------------------------------------------------------------------
# Severity lock: high-urgency entries stay on / move to the right specialist
# --------------------------------------------------------------------------
def _cardiology_high() -> dict:
    return {
        "urgency_level": "high",
        "recommended_specializations": [
            {"specialization": "Cardiology", "priority": 1, "reason": "chest pain"},
        ],
    }


async def test_high_urgency_entry_moves_from_a_generalist_to_a_specialist(session_factory):
    async with session_factory() as db:
        near = await _make_hospital(db, "Near", "H-SEV-N", NEAR)
        far = await _make_hospital(db, "Far", "H-SEV-F", FAR)
        gp = await _make_doctor(db, near, "D-SEV-GP", specialization="General Medicine")
        cardio = await _make_doctor(db, far, "D-SEV-CARD", specialization="Cardiology")

        # The generalist the patient is parked on is badly congested.
        await _fill_queue(db, gp, near, 40)

        patient = await _make_patient(db, "sev1")
        entry = QueueEntry(
            patient_id=patient.id,
            doctor_id=gp.id,
            hospital_id=near.id,
            queue_type=QueueType.CROSS_HOSPITAL,
            queue_number=41,
            status=QueueStatus.WAITING,
            priority=QueuePriority.HIGH,
            candidate_doctor_ids=[gp.id, cardio.id],
            ai_recommendation=_cardiology_high(),
            patient_latitude=NEAR[0],
            patient_longitude=NEAR[1],
        )
        db.add(entry)
        await db.commit()
        entry_id, cardio_id, far_id = entry.id, cardio.id, far.id

    async with session_factory() as db:
        entry = await db.get(QueueEntry, entry_id)
        assert await rescore_entry(db, entry) is True

    async with session_factory() as db:
        entry = await db.get(QueueEntry, entry_id)
        assert entry.doctor_id == cardio_id
        assert entry.hospital_id == far_id


async def test_high_urgency_entry_is_never_pulled_onto_a_generalist(session_factory):
    """Even when an empty generalist next door would score better, the severity
    lock keeps a Cardiology patient with a Cardiologist."""
    async with session_factory() as db:
        near = await _make_hospital(db, "Near", "H-SEV2-N", NEAR)
        far = await _make_hospital(db, "Far", "H-SEV2-F", FAR)
        gp = await _make_doctor(db, near, "D-SEV2-GP", specialization="General Medicine")
        cardio = await _make_doctor(db, far, "D-SEV2-CARD", specialization="Cardiology")

        # Cardiologist has a few ahead; generalist next door is empty.
        await _fill_queue(db, cardio, far, 4)

        patient = await _make_patient(db, "sev2")
        entry = QueueEntry(
            patient_id=patient.id,
            doctor_id=cardio.id,
            hospital_id=far.id,
            queue_type=QueueType.CROSS_HOSPITAL,
            queue_number=5,
            status=QueueStatus.WAITING,
            priority=QueuePriority.HIGH,
            candidate_doctor_ids=[gp.id, cardio.id],
            ai_recommendation=_cardiology_high(),
            patient_latitude=NEAR[0],
            patient_longitude=NEAR[1],
        )
        db.add(entry)
        await db.commit()
        entry_id, cardio_id = entry.id, cardio.id

    async with session_factory() as db:
        entry = await db.get(QueueEntry, entry_id)
        assert await rescore_entry(db, entry) is False
        assert entry.doctor_id == cardio_id


async def test_high_urgency_entry_stays_put_when_no_specialist_exists(session_factory):
    async with session_factory() as db:
        near = await _make_hospital(db, "Near", "H-SEV3-N", NEAR)
        far = await _make_hospital(db, "Far", "H-SEV3-F", FAR)
        gp_near = await _make_doctor(db, near, "D-SEV3-GP1", specialization="General Medicine")
        gp_far = await _make_doctor(db, far, "D-SEV3-GP2", specialization="General Medicine")
        await _fill_queue(db, gp_near, near, 40)

        patient = await _make_patient(db, "sev3")
        entry = QueueEntry(
            patient_id=patient.id,
            doctor_id=gp_near.id,
            hospital_id=near.id,
            queue_type=QueueType.CROSS_HOSPITAL,
            queue_number=41,
            status=QueueStatus.WAITING,
            priority=QueuePriority.HIGH,
            candidate_doctor_ids=[gp_near.id, gp_far.id],
            ai_recommendation=_cardiology_high(),
            patient_latitude=NEAR[0],
            patient_longitude=NEAR[1],
        )
        db.add(entry)
        await db.commit()
        entry_id, gp_near_id = entry.id, gp_near.id

    async with session_factory() as db:
        entry = await db.get(QueueEntry, entry_id)
        # No Cardiologist anywhere -> the sweep leaves the entry alone rather
        # than downgrading it to a generalist.
        assert await rescore_entry(db, entry) is False
        assert entry.doctor_id == gp_near_id
