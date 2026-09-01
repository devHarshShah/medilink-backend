"""Unit tests for the queue scoring engine.

Pure functions, no database. These pin the behaviour the whole cross-hospital
idea rests on: pick the centre that sees the patient soonest, tell them when to
leave, and stop moving them once they should be travelling.
"""

from datetime import UTC, datetime, timedelta

from src.services.queue.scoring import (
    UNKNOWN_TRAVEL_MINUTES,
    Candidate,
    compute_leave_at,
    haversine_km,
    is_severity_locked,
    is_worth_moving,
    rank_candidates,
    score_candidate,
    should_lock_in,
    top_specialization,
    travel_minutes,
    waiting_minutes,
)

# Two real Bengaluru points ~7km apart (MG Road and Koramangala).
MG_ROAD = (12.9750, 77.6060)
KORAMANGALA = (12.9352, 77.6245)


def _candidate(**kwargs) -> Candidate:
    base = dict(
        doctor_id=1,
        doctor_name="Dr. Test",
        specialization="General Medicine",
        hospital_id=1,
        hospital_name="Test Hospital",
        queue_size=5,
        avg_consultation_minutes=10.0,
        hospital_lat=MG_ROAD[0],
        hospital_lng=MG_ROAD[1],
        match_score=1.0,
    )
    base.update(kwargs)
    return Candidate(**base)


# --------------------------------------------------------------------------
# Distance and travel
# --------------------------------------------------------------------------
def test_haversine_matches_known_distance():
    km = haversine_km(*MG_ROAD, *KORAMANGALA)
    assert 4.0 < km < 6.0, km


def test_haversine_is_zero_for_the_same_point():
    assert haversine_km(*MG_ROAD, *MG_ROAD) == 0.0


def test_travel_time_is_none_without_coordinates():
    """None means unknown. Returning 0 would make an unlocatable hospital look
    infinitely close and win every comparison."""
    assert travel_minutes(None, None, *MG_ROAD) is None
    assert travel_minutes(*MG_ROAD, None, None) is None


def test_travel_time_grows_with_distance():
    near = travel_minutes(*MG_ROAD, *MG_ROAD)
    far = travel_minutes(*MG_ROAD, *KORAMANGALA)
    assert near == 0.0
    assert far > near


# --------------------------------------------------------------------------
# Waiting
# --------------------------------------------------------------------------
def test_waiting_scales_with_queue_depth():
    assert waiting_minutes(0, 10.0) == 0.0
    assert waiting_minutes(3, 10.0) == 30.0


def test_waiting_never_goes_negative():
    assert waiting_minutes(-2, 10.0) == 0.0


def test_zero_consultation_time_does_not_collapse_the_wait():
    """A doctor with a bogus 0-minute average must not make a 50-deep queue
    look instant."""
    assert waiting_minutes(50, 0.0) == 50.0


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------
def test_nearer_hospital_wins_when_queues_are_equal():
    near = _candidate(doctor_id=1, hospital_id=1, hospital_lat=MG_ROAD[0], hospital_lng=MG_ROAD[1])
    far = _candidate(doctor_id=2, hospital_id=2, hospital_lat=KORAMANGALA[0], hospital_lng=KORAMANGALA[1])
    ranked = rank_candidates([far, near], *MG_ROAD)
    assert ranked[0].candidate.doctor_id == 1


def test_shorter_queue_wins_when_distance_is_equal():
    busy = _candidate(doctor_id=1, queue_size=20)
    quiet = _candidate(doctor_id=2, queue_size=1)
    ranked = rank_candidates([busy, quiet], *MG_ROAD)
    assert ranked[0].candidate.doctor_id == 2


def test_a_long_queue_nearby_can_lose_to_a_short_queue_further_away():
    """The whole point of the cross-hospital queue."""
    near_busy = _candidate(doctor_id=1, queue_size=30, hospital_lat=MG_ROAD[0], hospital_lng=MG_ROAD[1])
    far_quiet = _candidate(
        doctor_id=2, queue_size=1, hospital_lat=KORAMANGALA[0], hospital_lng=KORAMANGALA[1]
    )
    ranked = rank_candidates([near_busy, far_quiet], *MG_ROAD)
    assert ranked[0].candidate.doctor_id == 2


def test_travel_is_not_free_so_a_marginal_gain_does_not_justify_a_trip():
    """A slightly shorter queue far away should not beat one next door."""
    next_door = _candidate(doctor_id=1, queue_size=6, hospital_lat=MG_ROAD[0], hospital_lng=MG_ROAD[1])
    slightly_shorter_far = _candidate(
        doctor_id=2, queue_size=5, hospital_lat=KORAMANGALA[0], hospital_lng=KORAMANGALA[1]
    )
    ranked = rank_candidates([next_door, slightly_shorter_far], *MG_ROAD)
    assert ranked[0].candidate.doctor_id == 1


def test_better_clinical_match_breaks_near_ties():
    weak_match = _candidate(doctor_id=1, queue_size=5, match_score=0.4)
    strong_match = _candidate(doctor_id=2, queue_size=5, match_score=1.0)
    ranked = rank_candidates([weak_match, strong_match], *MG_ROAD)
    assert ranked[0].candidate.doctor_id == 2


def test_hospital_without_coordinates_does_not_win_by_default():
    """A missing lat/lng must not read as distance zero."""
    unlocatable = _candidate(doctor_id=1, hospital_lat=None, hospital_lng=None, queue_size=5)
    located_here = _candidate(doctor_id=2, hospital_lat=MG_ROAD[0], hospital_lng=MG_ROAD[1], queue_size=5)
    ranked = rank_candidates([unlocatable, located_here], *MG_ROAD)
    assert ranked[0].candidate.doctor_id == 2


def test_ranking_without_patient_location_still_works():
    """Geolocation is optional in the browser; the queue must still function."""
    ranked = rank_candidates(
        [_candidate(doctor_id=1, queue_size=9), _candidate(doctor_id=2, queue_size=2)], None, None
    )
    assert ranked[0].candidate.doctor_id == 2
    assert ranked[0].travel_minutes is None
    assert ranked[0].distance_km is None


def test_ranking_is_deterministic_for_identical_candidates():
    a = _candidate(doctor_id=7)
    b = _candidate(doctor_id=3)
    assert [s.candidate.doctor_id for s in rank_candidates([a, b], *MG_ROAD)] == [3, 7]


def test_distance_is_reported_when_coordinates_exist():
    scored = score_candidate(_candidate(hospital_lat=KORAMANGALA[0], hospital_lng=KORAMANGALA[1]), *MG_ROAD)
    assert scored.distance_km is not None and scored.distance_km > 0


def test_total_minutes_is_the_later_of_wait_and_travel():
    scored = score_candidate(_candidate(queue_size=0), *KORAMANGALA)
    # Empty queue, so travel dominates.
    assert scored.total_minutes == scored.travel_minutes


# --------------------------------------------------------------------------
# Leave-at and lock-in
# --------------------------------------------------------------------------
def test_leave_at_is_wait_minus_travel_minus_buffer():
    now = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    # 120 min wait, 20 min travel, 10 min buffer -> leave in 90 minutes.
    leave = compute_leave_at(120.0, 20.0, now=now)
    assert leave == now + timedelta(minutes=90)


def test_leave_at_is_never_in_the_past():
    now = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    assert compute_leave_at(5.0, 40.0, now=now) == now


def test_leave_at_without_travel_estimate_uses_a_conservative_default():
    now = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    leave = compute_leave_at(120.0, None, now=now)
    expected = now + timedelta(minutes=120 - UNKNOWN_TRAVEL_MINUTES - 10)
    assert leave == expected


def test_lock_in_when_the_patient_should_be_travelling():
    assert should_lock_in(wait_minutes_value=15.0, travel_minutes_value=20.0) is True


def test_no_lock_in_while_there_is_time_to_spare():
    assert should_lock_in(wait_minutes_value=180.0, travel_minutes_value=20.0) is False


# --------------------------------------------------------------------------
# Hysteresis
# --------------------------------------------------------------------------
def test_marginal_improvement_does_not_trigger_a_move():
    """Without hysteresis a patient oscillates between two near-equal centres
    on every scoring pass."""
    assert is_worth_moving(current_score=60.0, best_score=58.0) is False


def test_clear_improvement_triggers_a_move():
    assert is_worth_moving(current_score=90.0, best_score=40.0) is True


def test_a_worse_option_never_triggers_a_move():
    assert is_worth_moving(current_score=30.0, best_score=80.0) is False


# --------------------------------------------------------------------------
# Severity lock
# --------------------------------------------------------------------------
def _reco(urgency: str, *specs: tuple[str, int]) -> dict:
    return {
        "urgency_level": urgency,
        "recommended_specializations": [
            {"specialization": name, "priority": pr, "reason": "x"} for name, pr in specs
        ],
    }


def test_severity_lock_triggers_only_for_emergency_and_high():
    assert is_severity_locked(_reco("emergency", ("Cardiology", 1))) is True
    assert is_severity_locked(_reco("high", ("Cardiology", 1))) is True
    assert is_severity_locked(_reco("medium", ("Cardiology", 1))) is False
    assert is_severity_locked(_reco("low", ("Cardiology", 1))) is False


def test_severity_lock_is_false_without_a_triage_result():
    assert is_severity_locked(None) is False
    assert is_severity_locked({}) is False


def test_top_specialization_picks_the_lowest_priority_number():
    reco = _reco("high", ("General Medicine", 3), ("Cardiology", 1), ("Pulmonology", 2))
    assert top_specialization(reco) == "Cardiology"


def test_top_specialization_is_none_when_absent():
    assert top_specialization(None) is None
    assert top_specialization({}) is None
    assert top_specialization({"recommended_specializations": []}) is None
    assert top_specialization({"recommended_specializations": [{"priority": 1}]}) is None
