"""Claude-backed triage.

Uses structured outputs (output_config.format) so the response is a validated
JSON object rather than prose we have to parse. Every failure path -- no key,
timeout, refusal, malformed output -- returns None so the caller can fall back
to the rule engine. This provider never raises into a request.
"""

import json
import logging
from typing import Any

import anthropic

from src.core.config import settings
from src.models.enums import UrgencyLevel
from src.schemas.ai import SymptomsAnalysisRequest, SymptomsAnalysisResponse
from src.services.triage.base import SPECIALIZATIONS

logger = logging.getLogger(__name__)

TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recommended_specializations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "specialization": {"type": "string", "enum": SPECIALIZATIONS},
                    "priority": {"type": "integer", "enum": [1, 2, 3]},
                    "reason": {"type": "string"},
                },
                "required": ["specialization", "priority", "reason"],
                "additionalProperties": False,
            },
        },
        "urgency_level": {"type": "string", "enum": ["emergency", "high", "medium", "low"]},
        "summary": {"type": "string"},
        "reasoning": {"type": "string"},
        "confidence_score": {"type": "number"},
        "additional_questions": {"type": "array", "items": {"type": "string"}},
        "red_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "recommended_specializations",
        "urgency_level",
        "summary",
        "reasoning",
        "confidence_score",
        "additional_questions",
        "red_flags",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = f"""You are the triage service for MediLink, a cross-hospital \
patient queueing platform in India. Patients describe symptoms in their own words; \
you decide which department should see them and how urgently.

You are routing, not diagnosing. Never name a specific disease as a conclusion, \
never suggest medication or dosage, and never tell someone their symptoms are \
nothing to worry about.

Departments available (use these exact strings, nothing else):
{chr(10).join(f"- {s}" for s in SPECIALIZATIONS)}

Rules:
- Recommend 1-3 departments. `priority` is 1 for the best match, then 2, then 3. \
Lower is better. Never give two departments the same priority.
- If nothing matches a specialist clearly, recommend General Medicine — a general \
physician can assess and refer. Do not invent a department.
- Set `urgency_level` to "emergency" only for conditions where waiting in a queue \
is itself dangerous: suspected heart attack or stroke, severe breathing difficulty, \
uncontrolled bleeding, loss of consciousness, seizures, anaphylaxis, poisoning, or \
active suicidal intent. For those, `summary` must tell the patient plainly to seek \
emergency care now instead of waiting.
- `red_flags` lists specific warning signs you found, in plain language. Empty if none.
- `additional_questions` are 2-4 short questions a doctor would want answered before \
the consultation. Ask about duration, progression, and current medication.
- `summary` is addressed to the patient, two sentences at most, calm and concrete.
- `reasoning` is for the clinician: what in the description drove the routing.
- `confidence_score` is 0.0-1.0 — how confident you are in the department choice, \
not in any diagnosis. Be honest when the description is vague.
- Err toward higher urgency when the description is ambiguous. Under-triage is far \
more costly than over-triage."""


class ClaudeTriageProvider:
    """Triage via the Claude API. Returns None on any failure."""

    name = "claude"

    def __init__(self) -> None:
        self._client: anthropic.AsyncAnthropic | None = None
        # Set once if the API rejects the server-side fallback beta, so we stop
        # sending it rather than burning a failed request on every call.
        self._fallbacks_supported = True
        if settings.anthropic_api_key:
            self._client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                timeout=settings.triage_timeout_seconds,
                max_retries=1,
            )

    def is_available(self) -> bool:
        return self._client is not None

    async def analyze(self, request: SymptomsAnalysisRequest) -> SymptomsAnalysisResponse | None:
        if self._client is None:
            return None

        payload = await self._call(request, with_fallbacks=self._fallbacks_supported)
        if payload is None:
            return None

        try:
            return self._to_response(payload)
        except Exception:
            logger.warning("Triage model returned unusable output; falling back", exc_info=True)
            return None

    async def _call(self, request: SymptomsAnalysisRequest, with_fallbacks: bool) -> dict | None:
        user_parts = [f"Symptoms: {request.symptoms_description}"]
        if request.patient_age is not None:
            user_parts.append(f"Age: {request.patient_age}")
        if request.patient_gender:
            user_parts.append(f"Gender: {request.patient_gender}")
        if request.medical_history:
            user_parts.append(f"Relevant history: {request.medical_history}")

        kwargs: dict[str, Any] = {
            "model": settings.triage_model,
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": "\n".join(user_parts)}],
            "output_config": {
                "format": {"type": "json_schema", "schema": TRIAGE_SCHEMA},
                # Triage sits in front of a booking flow, so latency is felt by
                # the patient. Low effort with thinking left on beats disabling
                # thinking outright.
                "effort": "low",
            },
        }

        try:
            if with_fallbacks:
                response = await self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                    **kwargs,
                )
            else:
                response = await self._client.messages.create(**kwargs)
        except anthropic.BadRequestError as exc:
            # Most likely the fallback beta is not enabled for this account.
            if with_fallbacks:
                logger.info("Server-side fallbacks unavailable, disabling: %s", exc)
                self._fallbacks_supported = False
                return await self._call(request, with_fallbacks=False)
            logger.warning("Triage request rejected: %s", exc)
            return None
        except anthropic.RateLimitError:
            logger.warning("Triage rate limited; using rule engine")
            return None
        except anthropic.APIConnectionError:
            logger.warning("Triage unreachable; using rule engine")
            return None
        except anthropic.APIError:
            logger.warning("Triage API error; using rule engine", exc_info=True)
            return None

        # Check the stop reason before touching content -- on a refusal the
        # content list is empty or partial and indexing it would raise.
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            logger.info("Triage request refused by safety classifier (category=%s)", category)
            return None

        for block in response.content:
            if block.type == "text":
                return json.loads(block.text)

        return None

    def _to_response(self, payload: dict) -> SymptomsAnalysisResponse:
        recommendations = payload.get("recommended_specializations") or []

        # The schema constrains the enum, but priorities can still collide.
        # Renumber by the order the model gave rather than trusting the field,
        # since the frontend ranks doctors by `4 - priority`.
        cleaned = []
        seen: set[str] = set()
        for index, item in enumerate(recommendations[:3]):
            specialization = item["specialization"]
            if specialization in seen:
                continue
            seen.add(specialization)
            cleaned.append(
                {
                    "specialization": specialization,
                    "priority": index + 1,
                    "reason": item.get("reason") or "Recommended by triage assessment.",
                }
            )

        if not cleaned:
            raise ValueError("model returned no usable specialisations")

        confidence = float(payload.get("confidence_score", 0.5))

        return SymptomsAnalysisResponse(
            recommended_specializations=cleaned,
            urgency_level=UrgencyLevel(payload["urgency_level"]),
            summary=payload["summary"],
            reasoning=payload["reasoning"],
            confidence_score=min(max(confidence, 0.0), 1.0),
            additional_questions=payload.get("additional_questions", [])[:4],
            red_flags=payload.get("red_flags", []),
            fallback_used=False,
        )
