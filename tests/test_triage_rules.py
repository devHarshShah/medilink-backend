"""Golden cases for the rule-based triage engine.

These pin the behaviour the queue depends on. The engine is a routing aid, so
the assertions check department and urgency -- never a diagnosis.
"""

import pytest

from src.models.enums import UrgencyLevel
from src.schemas.ai import SymptomsAnalysisRequest
from src.services.triage.rules import RuleTriageProvider

provider = RuleTriageProvider()


async def triage(description: str, **kwargs):
    return await provider.analyze(SymptomsAnalysisRequest(symptoms_description=description, **kwargs))


def top(result) -> str:
    return result.recommended_specializations[0].specialization


# (description, expected top specialisation)
ROUTING_CASES = [
    ("I have had a sore throat and earache for three days", "ENT"),
    ("Itchy red rash spreading across my arms and neck", "Dermatology"),
    ("My knee has been painful and swollen since I fell", "Orthopedics"),
    ("Burning acidity and heartburn after every meal", "Gastroenterology"),
    ("I feel anxious constantly and cannot sleep at night", "Psychiatry"),
    ("Persistent cough with wheezing, worse at night", "Pulmonology"),
    ("Irregular periods and pelvic pain", "Gynecology"),
    ("My blood sugar has been high and I feel tired all the time", "General Medicine"),
    ("Frequent headaches with dizziness for two weeks", "Neurology"),
    ("Heart racing and palpitations when I climb stairs", "Cardiology"),
]


@pytest.mark.parametrize(("description", "expected"), ROUTING_CASES)
async def test_routes_to_expected_specialization(description: str, expected: str):
    result = await triage(description)
    assert top(result) == expected, f"{description!r} -> {top(result)}"


EMERGENCY_CASES = [
    "Crushing chest pain radiating to my left arm, sweating",
    "My father's face is drooping and his speech is slurred",
    "I cannot breathe properly and I am gasping for air",
    "Vomiting blood since this morning",
    "He had a seizure and is now unresponsive",
    "I have been thinking about ending my life",
    "Sudden worst headache of my life",
    "Swollen tongue and my throat is closing after eating peanuts",
]


@pytest.mark.parametrize("description", EMERGENCY_CASES)
async def test_red_flags_force_emergency(description: str):
    result = await triage(description)
    assert result.urgency_level is UrgencyLevel.EMERGENCY, description
    assert result.red_flags, "an emergency must explain why"
    assert "emergency" in result.summary.lower()


async def test_low_urgency_for_routine_description():
    result = await triage("Mild occasional acidity, would like a routine check up")
    assert result.urgency_level is UrgencyLevel.LOW


async def test_high_urgency_for_severe_wording():
    result = await triage("Severe unbearable stomach ache that is getting worse")
    assert result.urgency_level is UrgencyLevel.HIGH


async def test_chronic_complaint_is_not_urgent():
    result = await triage("I have had mild joint pain for the past several months")
    assert result.urgency_level is UrgencyLevel.LOW
    assert top(result) == "Orthopedics"


async def test_severity_beats_chronic_duration():
    """A long-standing complaint that is now severe must not be downgraded."""
    result = await triage("I have had back pain for several months but it is now excruciating")
    assert result.urgency_level is UrgencyLevel.HIGH


async def test_child_routes_to_pediatrics():
    result = await triage("My 4 year old daughter has a fever and a cough", patient_age=4)
    assert top(result) == "Pediatrics"


async def test_age_overrides_organ_specialist_for_children():
    """A child's cough is Paediatrics, not Pulmonology, however many
    respiratory keywords matched."""
    result = await triage("Coughing and wheezing for two days", patient_age=6)
    assert top(result) == "Pediatrics"


async def test_unmatched_symptoms_fall_back_to_general_medicine():
    result = await triage("I just feel a bit off and cannot describe it properly")
    assert top(result) == "General Medicine"
    assert result.confidence_score < 0.6, "vague input should report low confidence"


async def test_priorities_are_unique_and_ordered():
    result = await triage("Chest tightness, palpitations, breathless, cough and fever")
    priorities = [r.priority for r in result.recommended_specializations]
    assert priorities == sorted(priorities)
    assert len(priorities) == len(set(priorities)), "duplicate priorities break frontend ranking"
    assert all(1 <= p <= 3 for p in priorities)


async def test_never_recommends_an_unstaffed_specialization():
    from src.services.triage.base import SPECIALIZATIONS

    for description, _ in ROUTING_CASES:
        result = await triage(description)
        for rec in result.recommended_specializations:
            assert rec.specialization in SPECIALIZATIONS


async def test_rule_engine_always_marks_itself_as_fallback():
    result = await triage("Sore throat")
    assert result.fallback_used is True


async def test_medical_history_is_considered():
    result = await triage(
        "Feeling short of breath climbing stairs",
        medical_history="Diagnosed with high blood pressure last year",
    )
    specializations = [r.specialization for r in result.recommended_specializations]
    assert "Cardiology" in specializations
