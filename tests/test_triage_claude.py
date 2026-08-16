"""Offline tests for the Claude provider's response handling.

These cover everything except the network call itself: schema shape, and the
normalisation that protects the frontend from a model that returns something
slightly off. The live API path is not exercised here.
"""

import json

import pytest

from src.models.enums import UrgencyLevel
from src.services.triage.base import SPECIALIZATIONS
from src.services.triage.claude import TRIAGE_SCHEMA, ClaudeTriageProvider

provider = ClaudeTriageProvider()


def _payload(**overrides) -> dict:
    base = {
        "recommended_specializations": [
            {"specialization": "Cardiology", "priority": 1, "reason": "Chest pain"},
        ],
        "urgency_level": "high",
        "summary": "You should be seen soon.",
        "reasoning": "Chest pain with exertion.",
        "confidence_score": 0.8,
        "additional_questions": ["How long?"],
        "red_flags": [],
    }
    base.update(overrides)
    return base


def test_schema_only_permits_staffed_specializations():
    enum = TRIAGE_SCHEMA["properties"]["recommended_specializations"]["items"]["properties"][
        "specialization"
    ]["enum"]
    assert enum == SPECIALIZATIONS


def test_schema_is_strict():
    """additionalProperties must be false or structured outputs won't compile."""
    assert TRIAGE_SCHEMA["additionalProperties"] is False
    items = TRIAGE_SCHEMA["properties"]["recommended_specializations"]["items"]
    assert items["additionalProperties"] is False


def test_schema_is_json_serialisable():
    json.dumps(TRIAGE_SCHEMA)


def test_normalises_a_well_formed_payload():
    result = provider._to_response(_payload())
    assert result.urgency_level is UrgencyLevel.HIGH
    assert result.fallback_used is False
    assert result.recommended_specializations[0].specialization == "Cardiology"


def test_duplicate_specializations_are_collapsed():
    result = provider._to_response(
        _payload(
            recommended_specializations=[
                {"specialization": "Cardiology", "priority": 1, "reason": "a"},
                {"specialization": "Cardiology", "priority": 2, "reason": "b"},
                {"specialization": "Pulmonology", "priority": 3, "reason": "c"},
            ]
        )
    )
    names = [r.specialization for r in result.recommended_specializations]
    assert names == ["Cardiology", "Pulmonology"]


def test_colliding_priorities_are_renumbered():
    """The frontend ranks by `4 - priority`; duplicates would tie and scramble
    the doctor list, so priorities are rewritten from the model's ordering."""
    result = provider._to_response(
        _payload(
            recommended_specializations=[
                {"specialization": "Cardiology", "priority": 1, "reason": "a"},
                {"specialization": "Pulmonology", "priority": 1, "reason": "b"},
                {"specialization": "Neurology", "priority": 1, "reason": "c"},
            ]
        )
    )
    assert [r.priority for r in result.recommended_specializations] == [1, 2, 3]


def test_more_than_three_recommendations_are_truncated():
    result = provider._to_response(
        _payload(
            recommended_specializations=[
                {"specialization": s, "priority": 1, "reason": "x"}
                for s in ["Cardiology", "Pulmonology", "Neurology", "ENT"]
            ]
        )
    )
    assert len(result.recommended_specializations) == 3


def test_out_of_range_confidence_is_clamped():
    assert provider._to_response(_payload(confidence_score=1.7)).confidence_score == 1.0
    assert provider._to_response(_payload(confidence_score=-0.2)).confidence_score == 0.0


def test_empty_recommendations_raise_so_the_caller_falls_back():
    with pytest.raises(ValueError):
        provider._to_response(_payload(recommended_specializations=[]))


def test_missing_reason_gets_a_default():
    result = provider._to_response(
        _payload(recommended_specializations=[{"specialization": "ENT", "priority": 1}])
    )
    assert result.recommended_specializations[0].reason


def test_questions_are_capped_at_four():
    result = provider._to_response(_payload(additional_questions=[f"q{i}" for i in range(9)]))
    assert len(result.additional_questions) == 4
