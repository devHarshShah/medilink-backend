"""Triage provider contract.

Two implementations sit behind this: Claude, and a deterministic rule engine.
The queue must never block on an LLM, so every call path is required to degrade
to the rule engine rather than raise.
"""

from typing import Protocol

from src.schemas.ai import SymptomsAnalysisRequest, SymptomsAnalysisResponse

# The specialisations the platform actually staffs. The LLM is constrained to
# this list -- a recommendation for a specialisation no hospital employs would
# produce an empty doctor list at the next step of the booking wizard.
SPECIALIZATIONS: list[str] = [
    "General Medicine",
    "Cardiology",
    "Dermatology",
    "Orthopedics",
    "Pediatrics",
    "Neurology",
    "ENT",
    "Gynecology",
    "Gastroenterology",
    "Pulmonology",
    "Psychiatry",
]

FALLBACK_SPECIALIZATION = "General Medicine"


class TriageProvider(Protocol):
    name: str

    async def analyze(self, request: SymptomsAnalysisRequest) -> SymptomsAnalysisResponse:
        """Map symptoms to specialisations and an urgency level."""
        ...

    def is_available(self) -> bool:
        """Whether this provider can currently serve a request."""
        ...
