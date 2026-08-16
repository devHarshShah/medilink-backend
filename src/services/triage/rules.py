"""Deterministic rule-based triage.

This is the floor the whole queue stands on: it runs when there is no API key,
when Claude times out, and in every test. It is intentionally boring and
keyword-driven -- no network, no randomness, no I/O.

It is a routing aid, not a diagnosis. When it is unsure it routes to General
Medicine rather than guessing a specialist, and it errs toward *higher* urgency
because the cost of under-triaging chest pain is not symmetric with the cost of
sending someone to a shorter queue than they needed.
"""

import re

from src.models.enums import UrgencyLevel
from src.schemas.ai import DoctorRecommendation, SymptomsAnalysisResponse
from src.services.triage.base import FALLBACK_SPECIALIZATION

# Phrases that mean "this person may not have hours to wait". Matching any of
# these forces emergency urgency regardless of anything else in the text.
RED_FLAGS: list[tuple[str, str]] = [
    (r"\bchest pain\b|\bchest tightness\b|\bcrushing chest\b", "Chest pain - possible cardiac event"),
    (r"\bcan(?:'|no)?t breathe\b|\bunable to breathe\b|\bgasping\b", "Severe breathing difficulty"),
    (r"\bshortness of breath at rest\b|\bsevere breathlessness\b", "Severe breathlessness"),
    (
        r"\bface (?:is |has )?droop\w*\b|\bslurred speech\b|\bone side.{0,20}(?:weak|numb)\b",
        "Possible stroke (FAST signs)",
    ),
    (r"\bsudden.{0,20}(?:weakness|numbness|paralysis)\b", "Sudden neurological deficit"),
    (r"\bunconscious\b|\bfainted\b|\bpassed out\b|\bunresponsive\b", "Loss of consciousness"),
    (r"\bseizure\b|\bconvulsion\b|\bfitting\b", "Seizure activity"),
    (
        r"\bheavy bleeding\b|\buncontrolled bleeding\b|\bbleeding (?:a lot|profusely)\b",
        "Uncontrolled bleeding",
    ),
    (r"\bcoughing (?:up )?blood\b|\bvomiting blood\b|\bblood in vomit\b", "Haemorrhage"),
    (
        r"\bsuicid\w*\b|\bkill(?:ing)? myself\b|\bend(?:ing)? (?:my|his|her|their) life\b|\bself[- ]harm\b",
        "Suicidal ideation - urgent psychiatric risk",
    ),
    (r"\bworst headache\b|\bthunderclap headache\b", "Thunderclap headache - possible haemorrhage"),
    (r"\bstiff neck\b.{0,40}\bfever\b|\bfever\b.{0,40}\bstiff neck\b", "Possible meningitis"),
    (r"\bsevere abdominal pain\b|\brigid abdomen\b", "Acute abdomen"),
    (r"\banaphyla\w*\b|\bthroat closing\b|\bswollen tongue\b", "Possible anaphylaxis"),
    (r"\bpoison\w*\b|\boverdose\b", "Poisoning or overdose"),
]

# specialisation -> (regex, human-readable reason)
SYMPTOM_RULES: dict[str, list[tuple[str, str]]] = {
    "Cardiology": [
        (r"\bchest pain\b|\bchest tightness\b|\bangina\b", "Chest pain suggests cardiac evaluation"),
        (r"\bpalpitation\w*\b|\bheart racing\b|\birregular heartbeat\b", "Reported palpitations"),
        (r"\bhigh blood pressure\b|\bhypertension\b|\bbp (?:is )?high\b", "Blood pressure concern"),
        (r"\bswollen (?:ankle|feet|leg)\w*\b|\bswelling in (?:legs|feet)\b", "Peripheral oedema"),
        (r"\bbreathless\w*\b.{0,30}\b(?:lying down|exertion|walking)\b", "Exertional breathlessness"),
    ],
    "Pulmonology": [
        (r"\bcough\w*\b", "Persistent cough"),
        (r"\bwheez\w*\b|\basthma\b", "Wheezing or asthma history"),
        (r"\bshortness of breath\b|\bbreathless\w*\b|\bdifficulty breathing\b", "Breathing difficulty"),
        (r"\bcopd\b|\bbronchitis\b|\bpneumonia\b", "Lower respiratory condition"),
    ],
    "Neurology": [
        (r"\bheadache\w*\b|\bmigraine\w*\b", "Headache"),
        (r"\bdizz\w*\b|\bvertigo\b|\blight[- ]headed\b", "Dizziness or vertigo"),
        (r"\bnumbness\b|\btingling\b|\bpins and needles\b", "Sensory symptoms"),
        (r"\bseizure\b|\bepilep\w*\b|\bconvulsion\b", "Seizure activity"),
        (r"\btremor\w*\b|\bshaking hands\b", "Tremor"),
        (r"\bmemory loss\b|\bconfusion\b|\bforgetful\w*\b", "Cognitive symptoms"),
    ],
    "Gastroenterology": [
        (r"\bstomach ache\b|\babdominal pain\b|\bstomach pain\b|\btummy pain\b", "Abdominal pain"),
        (r"\bnausea\b|\bvomit\w*\b", "Nausea or vomiting"),
        (r"\bdiarrh\w*\b|\bloose motion\w*\b", "Diarrhoea"),
        (r"\bconstipat\w*\b", "Constipation"),
        (r"\bacidity\b|\bheartburn\b|\bacid reflux\b|\bgerd\b", "Reflux symptoms"),
        (r"\bjaundice\b|\byellow(?:ing)? (?:of )?(?:eyes|skin)\b", "Jaundice"),
        (r"\bblood in stool\b|\bblack stool\b", "GI bleeding"),
    ],
    "Orthopedics": [
        # Joint words and pain words rarely sit adjacent in how people actually
        # write ("my knee has been painful"), so match them in proximity.
        (
            r"\b(?:joint|knee|shoulder|hip|wrist|elbow|ankle)\b.{0,30}\b(?:pain\w*|ache\w*|hurt\w*|sore)\b"
            r"|\b(?:pain\w*|ache\w*|hurt\w*|sore)\b.{0,30}\b(?:joint|knee|shoulder|hip|wrist|elbow|ankle)\b",
            "Joint pain",
        ),
        (r"\bback pain\b|\bneck pain\b|\bspine\b|\bsciatica\b|\bback (?:is )?hurt\w*\b", "Spinal pain"),
        (
            r"\bfracture\b|\bbroken bone\b|\bsprain\w*\b|\bi fell\b|\bfell (?:down|over)\b",
            "Injury or suspected fracture",
        ),
        (r"\barthritis\b|\bstiff joint\w*\b", "Arthritic symptoms"),
        (
            r"\bswollen\b.{0,30}\b(?:knee|joint|wrist|elbow|ankle|shoulder)\b"
            r"|\b(?:knee|joint|wrist|elbow|ankle|shoulder)\b.{0,30}\bswollen\b",
            "Joint swelling",
        ),
    ],
    "Dermatology": [
        (r"\brash\w*\b|\bskin irritation\b", "Skin rash"),
        (r"\bitch\w*\b|\bpruritus\b", "Itching"),
        (r"\bacne\b|\bpimple\w*\b", "Acne"),
        (r"\beczema\b|\bpsoriasis\b|\bdermatitis\b", "Chronic skin condition"),
        (r"\bmole\b|\bskin (?:lesion|growth|patch)\b", "Skin lesion"),
        (r"\bhair (?:loss|fall)\b|\bbald\w*\b", "Hair loss"),
    ],
    "ENT": [
        (r"\bsore throat\b|\bthroat pain\b|\btonsil\w*\b", "Throat pain"),
        (r"\bear ache\b|\bear pain\b|\bear infection\b|\bearache\b", "Ear pain"),
        (r"\bhearing loss\b|\bringing in (?:the )?ears?\b|\btinnitus\b", "Hearing symptoms"),
        (r"\bblocked nose\b|\brunny nose\b|\bsinus\w*\b|\bnasal congestion\b", "Nasal or sinus symptoms"),
        (r"\bhoarse\w*\b|\bvoice change\b|\blost my voice\b", "Voice change"),
    ],
    "Pediatrics": [
        # Allow words between the possessive and the relation: people write
        # "my 4 year old daughter", not "my daughter".
        (
            r"\bmy\b.{0,20}\b(?:child|son|daughter|baby|toddler|kid)\b",
            "Patient is a child",
        ),
        (r"\binfant\b|\bnewborn\b|\bmonths old\b|\b\d{1,2} year old\b", "Paediatric age"),
    ],
    "Gynecology": [
        (r"\bperiod\w*\b|\bmenstrua\w*\b|\bmenopause\b", "Menstrual symptoms"),
        (r"\bpregnan\w*\b", "Pregnancy-related"),
        (r"\bvaginal\b|\bpelvic pain\b", "Gynaecological symptoms"),
        (r"\bcontraception\b|\bbirth control\b", "Contraception"),
    ],
    "Psychiatry": [
        (r"\bdepress\w*\b|\bhopeless\b|\bno interest in\b", "Depressive symptoms"),
        (r"\banxi\w*\b|\bpanic attack\w*\b|\bworr(?:y|ied) all the time\b", "Anxiety symptoms"),
        (r"\binsomnia\b|\bcan(?:'|no)?t sleep\b|\btrouble sleeping\b", "Sleep disturbance"),
        (r"\bstress\w*\b|\bburn(?:t|ed) out\b", "Stress"),
        (r"\bsuicid\w*\b|\bself[- ]harm\b", "Suicidal ideation"),
    ],
    "General Medicine": [
        (r"\bfever\b|\btemperature\b|\bchills\b", "Fever"),
        (r"\bfatigue\b|\btired\w*\b|\bweak(?:ness)?\b", "Fatigue"),
        (r"\bbody ache\w*\b|\bmuscle ache\w*\b", "Generalised aches"),
        (r"\bcold\b|\bflu\b|\bviral\b", "Viral illness"),
        (r"\bdiabet\w*\b|\bblood sugar\b", "Diabetes management"),
        (r"\bthyroid\b", "Thyroid concern"),
        (r"\bcheck ?up\b|\broutine\b|\bfollow[- ]up\b", "Routine review"),
        (r"\bweight (?:loss|gain)\b", "Weight change"),
    ],
}

# Words that raise urgency without being outright red flags.
HIGH_URGENCY = re.compile(
    r"\bsevere\b|\bexcruciating\b|\bunbearable\b|\bworst\b|\bintense\b|\bagon\w*\b"
    r"|\bgetting worse\b|\brapidly\b|\bsudden\w*\b|\bhigh fever\b|\b10[/ ]10\b",
    re.IGNORECASE,
)
LOW_URGENCY = re.compile(
    r"\bmild\b|\bslight\w*\b|\boccasional\w*\b|\bcheck ?up\b|\broutine\b"
    r"|\bfor (?:a )?few months\b|\bcomes and goes\b|\bnot (?:too )?bad\b|\bmanageable\b",
    re.IGNORECASE,
)
CHRONIC = re.compile(
    r"\bfor (?:the last |the past )?(?:\d+|a few|several) (?:weeks?|months?|years?)\b|\bchronic\b",
    re.IGNORECASE,
)

FOLLOW_UP_QUESTIONS = [
    "How long have you had these symptoms?",
    "Have the symptoms got worse, stayed the same, or improved?",
    "Are you currently taking any medication for this?",
    "Have you had this problem before?",
]


class RuleTriageProvider:
    """Keyword and heuristic triage. Always available, never raises."""

    name = "rules"

    def is_available(self) -> bool:
        return True

    async def analyze(self, request) -> SymptomsAnalysisResponse:
        text = request.symptoms_description.lower()
        history = (request.medical_history or "").lower()
        haystack = f"{text} {history}"

        red_flags = [label for pattern, label in RED_FLAGS if re.search(pattern, haystack, re.IGNORECASE)]

        # Score each specialisation by how many of its rules matched.
        scores: dict[str, list[str]] = {}
        for specialization, rules in SYMPTOM_RULES.items():
            reasons = [reason for pattern, reason in rules if re.search(pattern, haystack, re.IGNORECASE)]
            if reasons:
                scores[specialization] = reasons

        ranked = sorted(scores.items(), key=lambda item: (-len(item[1]), item[0]))

        # Age is decisive, not just another matching rule: a 6-year-old with a
        # cough belongs in Paediatrics however many respiratory keywords hit.
        # Promote rather than append, or keyword count silently outvotes age.
        is_child = request.patient_age is not None and request.patient_age < 16
        if is_child:
            reasons = scores.get("Pediatrics", [])
            reasons.append(f"Patient is {request.patient_age} years old")
            ranked = [("Pediatrics", reasons)] + [item for item in ranked if item[0] != "Pediatrics"]

        recommendations: list[DoctorRecommendation] = []
        for index, (specialization, reasons) in enumerate(ranked[:3]):
            recommendations.append(
                DoctorRecommendation(
                    specialization=specialization,
                    priority=index + 1,
                    reason="; ".join(reasons[:2]),
                )
            )

        if not recommendations:
            recommendations = [
                DoctorRecommendation(
                    specialization=FALLBACK_SPECIALIZATION,
                    priority=1,
                    reason=(
                        "Symptoms did not match a specialist pattern; "
                        "a general physician can assess and refer."
                    ),
                )
            ]

        urgency = self._urgency(haystack, red_flags)

        confidence = 0.35 if not scores else min(0.4 + 0.15 * len(ranked[0][1]), 0.8)
        if red_flags:
            confidence = max(confidence, 0.75)

        return SymptomsAnalysisResponse(
            recommended_specializations=recommendations,
            urgency_level=urgency,
            summary=self._summary(recommendations, urgency, red_flags),
            reasoning=(
                "Matched using MediLink's rule-based triage (keyword and severity heuristics). "
                "This routes you to an appropriate department; it is not a diagnosis."
            ),
            confidence_score=round(confidence, 2),
            additional_questions=FOLLOW_UP_QUESTIONS[:3],
            red_flags=red_flags,
            fallback_used=True,
        )

    def _urgency(self, haystack: str, red_flags: list[str]) -> UrgencyLevel:
        if red_flags:
            return UrgencyLevel.EMERGENCY
        if HIGH_URGENCY.search(haystack):
            return UrgencyLevel.HIGH
        # Check chronic before low: a long-standing complaint is rarely urgent,
        # but "mild" alone shouldn't downgrade something described as worsening.
        if CHRONIC.search(haystack) and not HIGH_URGENCY.search(haystack):
            return UrgencyLevel.LOW
        if LOW_URGENCY.search(haystack):
            return UrgencyLevel.LOW
        return UrgencyLevel.MEDIUM

    def _summary(
        self, recommendations: list[DoctorRecommendation], urgency: UrgencyLevel, red_flags: list[str]
    ) -> str:
        top = recommendations[0].specialization
        if urgency is UrgencyLevel.EMERGENCY:
            return (
                f"Your description includes signs that need urgent assessment ({red_flags[0]}). "
                f"Please seek emergency care now rather than waiting in a queue. "
                f"Suggested department: {top}."
            )
        if urgency is UrgencyLevel.HIGH:
            return f"Your symptoms suggest you should be seen soon. Suggested department: {top}."
        return f"Based on your description, {top} is the most appropriate department."
