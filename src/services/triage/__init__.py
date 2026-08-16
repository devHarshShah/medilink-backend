"""Triage service.

`analyze_symptoms` is the only entry point callers should use. It tries Claude
first and silently degrades to the rule engine, marking the response with
`fallback_used` so the UI and our own metrics can tell the two apart.
"""

import logging
import time

from src.schemas.ai import SymptomsAnalysisRequest, SymptomsAnalysisResponse
from src.services.triage.base import SPECIALIZATIONS, TriageProvider
from src.services.triage.claude import ClaudeTriageProvider
from src.services.triage.rules import RuleTriageProvider

logger = logging.getLogger(__name__)

_claude = ClaudeTriageProvider()
_rules = RuleTriageProvider()


async def analyze_symptoms(request: SymptomsAnalysisRequest) -> tuple[SymptomsAnalysisResponse, str, int]:
    """Return (analysis, provider_name, latency_ms)."""
    started = time.perf_counter()

    if _claude.is_available():
        # The provider is written not to raise, but this is the one path that
        # must never fail a request -- a triage 500 blocks the whole booking
        # flow. Belt and braces: catch anything it didn't.
        try:
            result = await _claude.analyze(request)
        except Exception:
            logger.warning("Triage provider raised unexpectedly; using rule engine", exc_info=True)
            result = None
        if result is not None:
            return result, _claude.name, int((time.perf_counter() - started) * 1000)

    result = await _rules.analyze(request)
    return result, _rules.name, int((time.perf_counter() - started) * 1000)


def active_provider() -> str:
    return _claude.name if _claude.is_available() else _rules.name


__all__ = [
    "SPECIALIZATIONS",
    "ClaudeTriageProvider",
    "RuleTriageProvider",
    "TriageProvider",
    "active_provider",
    "analyze_symptoms",
]
