"""Queue scoring: how long until a patient is seen, and when to set off.

Pure functions, no I/O -- everything here is unit-testable without a database.

The product idea is that a patient waits at home rather than in a corridor, so
the number that matters is not "how long is the queue" but "when should I
leave". Those differ: a busy hospital 5 minutes away can beat a quiet one an
hour away.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.core.config import settings
from src.models.enums import QueuePriority

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def travel_minutes(
    patient_lat: float | None,
    patient_lng: float | None,
    hospital_lat: float | None,
    hospital_lng: float | None,
) -> float | None:
    """Estimated travel time. None when either end has no coordinates -- callers
    must treat that as 'unknown', not as 'zero', or a hospital with no
    coordinates would look infinitely close and win every comparison."""
    if None in (patient_lat, patient_lng, hospital_lat, hospital_lng):
        return None
    distance = haversine_km(patient_lat, patient_lng, hospital_lat, hospital_lng)
    # Straight-line distance understates road distance; 1.3 is a common urban
    # detour factor.
    road_km = distance * 1.3
    return (road_km / settings.queue_travel_speed_kmh) * 60.0


def waiting_minutes(entries_ahead: int, avg_consultation_minutes: float) -> float:
    """How long until this patient's turn, given who is ahead of them."""
    return max(0.0, entries_ahead * max(avg_consultation_minutes, 1.0))


@dataclass(frozen=True)
class Candidate:
    """One (doctor x hospital) option under consideration."""

    doctor_id: int
    doctor_name: str
    specialization: str
    hospital_id: int
    hospital_name: str
    queue_size: int
    avg_consultation_minutes: float
    hospital_lat: float | None
    hospital_lng: float | None
    # From the triage result: 1.0 for the top-priority specialisation, less for
    # the runners-up. Nudges ties toward the better clinical match.
    match_score: float = 1.0


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    wait_minutes: float
    travel_minutes: float | None
    distance_km: float | None
    score: float

    @property
    def total_minutes(self) -> float:
        """When the patient would actually be seen, from now."""
        return max(self.wait_minutes, self.travel_minutes or 0.0)


# How much a minute of travelling counts against a minute of waiting at home.
# 1.0 means they cost the same, charged on top of the time travel displaces --
# so a centre only wins on distance if the time it actually saves exceeds the
# extra driving. Below 1.0 the engine would send patients across the city to
# save a couple of minutes.
TRAVEL_WEIGHT = 1.0
# Penalty in minutes for a poorer clinical match, so a marginally faster but
# less suitable doctor doesn't win on a rounding difference.
MATCH_PENALTY_MINUTES = 25.0
# Assumed travel when we have no coordinates. Deliberately non-zero: without it
# an unlocatable hospital would always look nearest.
UNKNOWN_TRAVEL_MINUTES = 30.0


def score_candidate(
    candidate: Candidate,
    patient_lat: float | None,
    patient_lng: float | None,
) -> ScoredCandidate:
    wait = waiting_minutes(candidate.queue_size, candidate.avg_consultation_minutes)
    travel = travel_minutes(patient_lat, patient_lng, candidate.hospital_lat, candidate.hospital_lng)

    distance = None
    if None not in (patient_lat, patient_lng, candidate.hospital_lat, candidate.hospital_lng):
        distance = haversine_km(patient_lat, patient_lng, candidate.hospital_lat, candidate.hospital_lng)

    effective_travel = travel if travel is not None else UNKNOWN_TRAVEL_MINUTES

    # The patient is seen at max(wait, travel) -- arriving early still means
    # waiting for the queue, and a long queue is dead time either way. Travel is
    # then weighted on top because time in a car is worse than time at home.
    score = (
        max(wait, effective_travel)
        + TRAVEL_WEIGHT * effective_travel
        + MATCH_PENALTY_MINUTES * (1.0 - candidate.match_score)
    )

    return ScoredCandidate(
        candidate=candidate,
        wait_minutes=wait,
        travel_minutes=travel,
        distance_km=round(distance, 2) if distance is not None else None,
        score=score,
    )


def rank_candidates(
    candidates: list[Candidate],
    patient_lat: float | None,
    patient_lng: float | None,
) -> list[ScoredCandidate]:
    """Best first. Ties break on doctor_id so ranking is deterministic."""
    scored = [score_candidate(c, patient_lat, patient_lng) for c in candidates]
    return sorted(scored, key=lambda s: (s.score, s.candidate.doctor_id))


def compute_leave_at(
    wait_minutes_value: float,
    travel_minutes_value: float | None,
    now: datetime | None = None,
) -> datetime:
    """When the patient should set off.

    Leaves a safety buffer, and never returns a time in the past -- if their
    turn is imminent the honest answer is 'leave now'.
    """
    now = now or datetime.now(UTC)
    travel = travel_minutes_value if travel_minutes_value is not None else UNKNOWN_TRAVEL_MINUTES
    lead = wait_minutes_value - travel - settings.queue_safety_buffer_minutes
    return now + timedelta(minutes=max(0.0, lead))


def should_lock_in(wait_minutes_value: float, travel_minutes_value: float | None) -> bool:
    """True once the patient needs to be travelling.

    Past this point the entry stops floating between hospitals: being re-routed
    mid-journey is worse than a slightly longer wait.
    """
    travel = travel_minutes_value if travel_minutes_value is not None else UNKNOWN_TRAVEL_MINUTES
    return wait_minutes_value <= travel + settings.queue_safety_buffer_minutes


def is_worth_moving(current_score: float, best_score: float) -> bool:
    """Only move for a clear improvement.

    Without hysteresis a patient would oscillate between two near-equal centres
    on every scoring pass.
    """
    return (current_score - best_score) > settings.queue_reassign_hysteresis_minutes


# Urgency levels at which doctor selection stops being a soft preference. Above
# this bar the match penalty is not enough -- a faster generalist must not win
# over the right specialist -- so assignment is hard-restricted to the top
# recommended specialisation (see engine.select_candidates).
SEVERITY_LOCK_URGENCIES = frozenset({"emergency", "high"})


def is_severity_locked(ai_recommendation: dict | None) -> bool:
    """True when triage urgency demands a specialist, not just prefers one."""
    return bool(ai_recommendation) and (
        ai_recommendation.get("urgency_level") in SEVERITY_LOCK_URGENCIES
    )


def top_specialization(ai_recommendation: dict | None) -> str | None:
    """The priority-1 specialisation from triage (lower priority int = better).

    Returns None when there is no triage result or it named no specialisation --
    callers then fall back to the ordinary soft-penalty ranking.
    """
    recs = (ai_recommendation or {}).get("recommended_specializations") or []
    ranked = sorted(
        (r for r in recs if isinstance(r, dict) and r.get("specialization")),
        key=lambda r: r.get("priority", 99),
    )
    return ranked[0]["specialization"] if ranked else None


def priority_rank(priority: QueuePriority) -> int:
    """Higher is served first."""
    return priority.weight
