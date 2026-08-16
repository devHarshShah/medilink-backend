import pytest_asyncio

from src.schemas.ai import SymptomsAnalysisRequest
from src.services.triage.claude import ClaudeTriageProvider


@pytest_asyncio.fixture
async def patient_headers(client, patient_payload):
    await client.post("/api/auth/register/patient", json=patient_payload)
    login = await client.post(
        "/api/auth/login",
        json={"email": patient_payload["email"], "password": patient_payload["password"]},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_analyze_requires_authentication(client):
    resp = await client.post(
        "/api/ai/analyze-symptoms", json={"symptoms_description": "I have a sore throat and fever"}
    )
    assert resp.status_code == 401


async def test_analyze_returns_the_frontend_contract(client, patient_headers):
    resp = await client.post(
        "/api/ai/analyze-symptoms",
        json={"symptoms_description": "Sore throat and earache for three days"},
        headers=patient_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Every field symptoms-analysis.tsx reads must be present.
    assert set(body) >= {
        "recommended_specializations",
        "urgency_level",
        "summary",
        "reasoning",
        "confidence_score",
        "additional_questions",
        "red_flags",
        "fallback_used",
    }
    assert body["urgency_level"] in {"emergency", "high", "medium", "low"}
    assert 0.0 <= body["confidence_score"] <= 1.0

    first = body["recommended_specializations"][0]
    assert set(first) == {"specialization", "priority", "reason"}
    assert 1 <= first["priority"] <= 3


async def test_short_description_is_rejected(client, patient_headers):
    """The form enforces a 10-character minimum; the server must too."""
    resp = await client.post(
        "/api/ai/analyze-symptoms",
        json={"symptoms_description": "hurts"},
        headers=patient_headers,
    )
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], list)


async def test_falls_back_to_rules_without_an_api_key(client, patient_headers):
    """No ANTHROPIC_API_KEY is set in tests, so this exercises the degraded path."""
    resp = await client.post(
        "/api/ai/analyze-symptoms",
        json={"symptoms_description": "Persistent cough with wheezing at night"},
        headers=patient_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["fallback_used"] is True


async def test_emergency_symptoms_surface_red_flags(client, patient_headers):
    resp = await client.post(
        "/api/ai/analyze-symptoms",
        json={"symptoms_description": "Crushing chest pain radiating to my left arm with sweating"},
        headers=patient_headers,
    )
    body = resp.json()
    assert body["urgency_level"] == "emergency"
    assert body["red_flags"]


async def test_analysis_is_persisted(client, patient_headers, session_factory):
    from sqlalchemy import select

    from src.models.triage import TriageAnalysis

    await client.post(
        "/api/ai/analyze-symptoms",
        json={"symptoms_description": "Itchy red rash spreading on my arms"},
        headers=patient_headers,
    )

    async with session_factory() as db:
        rows = (await db.execute(select(TriageAnalysis))).scalars().all()

    assert len(rows) == 1
    assert rows[0].provider == "rules"
    assert rows[0].patient_id is not None
    assert rows[0].response["recommended_specializations"]


async def test_health_reports_the_active_provider(client):
    resp = await client.get("/api/ai/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] in {"claude", "rules"}
    # The rule engine is always there, so triage is never fully unavailable.
    assert body["fallback_available"] is True


async def test_claude_provider_is_inactive_without_a_key():
    assert ClaudeTriageProvider().is_available() is False


async def test_claude_provider_returns_none_when_unconfigured():
    """The contract the fallback depends on: never raise, return None."""
    result = await ClaudeTriageProvider().analyze(
        SymptomsAnalysisRequest(symptoms_description="Chest pain and breathlessness")
    )
    assert result is None


async def test_falls_back_when_a_configured_claude_fails(client, patient_headers, monkeypatch):
    """The load-bearing guarantee: if Claude is configured but errors, the
    request still succeeds on the rule engine instead of 500-ing the queue."""
    import src.services.triage as triage_service

    async def _boom(_request):
        raise RuntimeError("simulated upstream failure")

    monkeypatch.setattr(triage_service._claude, "is_available", lambda: True)
    monkeypatch.setattr(triage_service._claude, "analyze", _boom)

    resp = await client.post(
        "/api/ai/analyze-symptoms",
        json={"symptoms_description": "Severe headache with dizziness for two days"},
        headers=patient_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["fallback_used"] is True
