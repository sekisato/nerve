from __future__ import annotations

from typing import Any, Protocol

import httpx

from .models import (
    DIMENSION_NAMES,
    DataSufficiency,
    DirectionState,
    InventoryTransition,
    SemanticDimension,
    SemanticVector,
    raw_json,
)

TYPESAFE_BASE_URL = "https://api.typesafe.ai"


class SemanticProviderValidationError(ValueError):
    def __init__(self, message: str, raw_answer_json: str) -> None:
        super().__init__(message)
        self.raw_answer_json = raw_answer_json


class SemanticProvider(Protocol):
    provider_name: str

    def evaluate(self, payload: dict[str, Any], *, model: str, timeout_seconds: float) -> tuple[SemanticVector, str, str]: ...


class TypeSafeJevProvider:
    provider_name = "typesafe-system-one"

    def __init__(self, api_key: str, *, base_url: str = TYPESAFE_BASE_URL) -> None:
        if not api_key.strip():
            raise ValueError("TYPESAFE_API_KEY is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def evaluate(
        self, payload: dict[str, Any], *, model: str, timeout_seconds: float
    ) -> tuple[SemanticVector, str, str]:
        response = httpx.post(
            f"{self._base_url}/v1/systemone",
            headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
            json={"state": payload, "model": model, "questions": semantic_questions()},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, dict):
            raise SemanticProviderValidationError(
                "Jev response must be a JSON object", raw_json(raw)
            )
        returned_model = raw.get("model")
        answers = raw.get("answers")
        usage = raw.get("usage")
        if (
            not isinstance(returned_model, str)
            or not isinstance(answers, dict)
            or not isinstance(usage, dict)
            or not isinstance(usage.get("input_tokens"), int)
            or not isinstance(usage.get("output_tokens"), int)
        ):
            raise SemanticProviderValidationError(
                "Jev response is missing model, answers, or usage", raw_json(raw)
            )
        try:
            vector = parse_semantic_answers(answers)
        except (KeyError, TypeError, ValueError) as exc:
            raise SemanticProviderValidationError(
                f"Jev response validation failed: {exc}", raw_json(raw)
            ) from exc
        return vector, returned_model, raw_json(raw)


def semantic_questions() -> dict[str, dict[str, Any]]:
    instruction = (
        "Use only supplied evidence. UNAVAILABLE is unknown and PARTIAL is incomplete. "
        "Same-slot does not prove coordination; shared funding does not prove bundling; "
        "early holdings do not prove sniping; creator history is bounded observed history. "
        "Do not recommend a trade."
    )
    directional = {
        "LOW": "The named tendency is weak or absent in supplied evidence.",
        "MEDIUM": "The named tendency is mixed or moderate in supplied evidence.",
        "HIGH": "The named tendency is strongly resembled by supplied evidence.",
        "INSUFFICIENT": "Coverage or evidence is insufficient for this classification.",
    }
    questions = {
        name: {
            "type": "choice",
            "instructions": f"{instruction} Classify {name}.",
            "criteria": directional,
        }
        for name in DIMENSION_NAMES
        if name != "inventory_transition"
    }
    questions["inventory_transition"] = {
        "type": "choice",
        "instructions": f"{instruction} Classify the observed inventory transition.",
        "criteria": {name: None for name in InventoryTransition},
    }
    questions["overall_data_sufficiency"] = {
        "type": "choice",
        "instructions": f"{instruction} Classify overall data sufficiency.",
        "criteria": {name: None for name in DataSufficiency},
    }
    questions["abstain"] = {
        "type": "choice",
        "instructions": f"{instruction} Select TRUE when semantic classification should abstain.",
        "criteria": {"TRUE": None, "FALSE": None},
    }
    return questions


def parse_semantic_answers(answers: dict[str, Any]) -> SemanticVector:
    expected = set(DIMENSION_NAMES) | {"overall_data_sufficiency", "abstain"}
    if set(answers) != expected:
        raise ValueError("Jev response has missing or unexpected answers")
    dimensions = []
    for name in DIMENSION_NAMES:
        answer = _choice(answers[name])
        state = (
            InventoryTransition(answer["choice"])
            if name == "inventory_transition"
            else DirectionState(answer["choice"])
        )
        dimensions.append(
            SemanticDimension(
                dimension_name=name,
                state=state,
                semantic_confidence=answer["confidence"],
            )
        )
    sufficiency = _choice(answers["overall_data_sufficiency"])["choice"]
    abstain = _choice(answers["abstain"])["choice"]
    return SemanticVector(
        overall_data_sufficiency=DataSufficiency(sufficiency),
        abstain={"TRUE": True, "FALSE": False}[abstain],
        dimensions=tuple(dimensions),
    )


def _choice(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("type") != "choice":
        raise ValueError("every semantic answer must be a choice")
    choice = value.get("choice")
    confidence = value.get("confidence")
    probabilities = value.get("probabilities")
    if not isinstance(choice, str) or isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("choice answer is malformed")
    if not 0 <= float(confidence) <= 1:
        raise ValueError("semantic confidence must be between 0 and 1")
    if not isinstance(probabilities, dict) or choice not in probabilities:
        raise ValueError("choice probabilities are malformed")
    probability_values = list(probabilities.values())
    if not probability_values or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not 0 <= item <= 1
        for item in probability_values
    ):
        raise ValueError("choice probabilities are malformed")
    if not 0.98 <= sum(float(item) for item in probability_values) <= 1.02:
        raise ValueError("choice probabilities must sum approximately to one")
    return {"choice": choice, "confidence": float(confidence)}
