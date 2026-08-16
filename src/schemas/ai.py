"""AI triage schemas.

Shapes are fixed by the frontend: src/types/api.ts declares
SymptomsAnalysisRequest / SymptomsAnalysisResponse, and
components/symptoms-analysis.tsx renders the fields directly.
"""

from pydantic import BaseModel, Field

from src.models.enums import UrgencyLevel


class SymptomsAnalysisRequest(BaseModel):
    symptoms_description: str = Field(min_length=10, max_length=2000)
    patient_age: int | None = Field(default=None, ge=0, le=130)
    patient_gender: str | None = None
    medical_history: str | None = None


class DoctorRecommendation(BaseModel):
    specialization: str
    # 1..3, LOWER IS BETTER. book-consultation/page.tsx weights matches as
    # `4 - priority`, so inverting this silently ranks the worst doctor first.
    priority: int = Field(ge=1, le=3)
    reason: str


class SymptomsAnalysisResponse(BaseModel):
    recommended_specializations: list[DoctorRecommendation]
    urgency_level: UrgencyLevel
    summary: str
    reasoning: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    additional_questions: list[str] = []
    red_flags: list[str] = []
    fallback_used: bool = False


class TriageHealthResponse(BaseModel):
    status: str
    provider: str
    model: str | None = None
    fallback_available: bool
