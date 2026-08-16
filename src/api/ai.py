import logging

from fastapi import APIRouter
from sqlalchemy import select

from src.core.config import settings
from src.core.deps import CurrentUser, DbSession
from src.models.enums import UserRole
from src.models.people import Patient
from src.models.triage import TriageAnalysis
from src.schemas.ai import SymptomsAnalysisRequest, SymptomsAnalysisResponse, TriageHealthResponse
from src.services.triage import active_provider, analyze_symptoms

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["AI"])


@router.post("/analyze-symptoms", response_model=SymptomsAnalysisResponse)
async def analyze_symptoms_endpoint(
    payload: SymptomsAnalysisRequest,
    user: CurrentUser,
    db: DbSession,
):
    """Map a free-text symptom description to specialisations and an urgency level.

    Routing only -- this is not a diagnosis, and the response is worded for a
    patient who is about to pick a queue.
    """
    patient: Patient | None = None
    if user.role == UserRole.PATIENT:
        result = await db.execute(select(Patient).where(Patient.user_id == user.id))
        patient = result.scalar_one_or_none()

    # Fill in age from the patient record when the client didn't send it --
    # age changes paediatric routing, and the booking form never asks for it.
    request = payload
    if patient is not None and request.patient_age is None:
        from datetime import date

        today = date.today()
        dob = patient.date_of_birth
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        request = payload.model_copy(
            update={
                "patient_age": age,
                "patient_gender": payload.patient_gender or patient.gender.value,
                "medical_history": payload.medical_history or patient.chronic_conditions,
            }
        )

    analysis, provider, latency_ms = await analyze_symptoms(request)

    # Persistence is for tuning, not for serving -- never fail the request over it.
    try:
        db.add(
            TriageAnalysis(
                patient_id=patient.id if patient else None,
                symptoms_description=request.symptoms_description,
                patient_age=request.patient_age,
                patient_gender=request.patient_gender,
                medical_history=request.medical_history,
                provider=provider,
                model=settings.triage_model if provider == "claude" else None,
                latency_ms=latency_ms,
                urgency_level=analysis.urgency_level,
                confidence_score=analysis.confidence_score,
                response=analysis.model_dump(mode="json"),
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("Could not persist triage analysis", exc_info=True)

    return analysis


@router.get("/health", response_model=TriageHealthResponse)
async def triage_health():
    """Which triage provider is live. The rule engine is always available, so
    this reports degraded rather than unhealthy when Claude is not configured."""
    provider = active_provider()
    return TriageHealthResponse(
        status="healthy" if provider == "claude" else "degraded",
        provider=provider,
        model=settings.triage_model if provider == "claude" else None,
        fallback_available=True,
    )
