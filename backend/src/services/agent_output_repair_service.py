from __future__ import annotations

import json
from typing import Any, Optional


class AgentOutputRepairService:
    def __init__(self, provider: Any, max_attempts: int = 1):
        self.provider = provider
        self.max_attempts = max(0, min(int(max_attempts), 2))

    def repair(
        self,
        raw_response: str,
        error: Exception,
        schema_name: str,
        request_context: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[str], dict[str, Any]]:
        metadata = {
            "attemptCount": 0,
            "status": "skipped",
            "errorType": error.__class__.__name__,
            "parserErrorMessage": str(error),
            "schemaName": schema_name,
        }
        method_name = "repair_initial_plan" if schema_name == "AgentInitialPlanOutput" else "repair_structured_output"
        repair_method = getattr(self.provider, method_name, None)
        if repair_method is None or self.max_attempts <= 0:
            metadata["failureReason"] = "provider_repair_method_missing"
            return None, metadata
        prompt = self._prompt(raw_response, error, schema_name, request_context or {})
        last_error: Optional[str] = None
        for attempt in range(1, self.max_attempts + 1):
            metadata["attemptCount"] = attempt
            try:
                repaired = repair_method(prompt)
            except Exception as repair_error:
                last_error = str(repair_error)
                continue
            if isinstance(repaired, str) and repaired.strip():
                metadata["status"] = "completed"
                return repaired, metadata
            last_error = "repair_provider_returned_empty"
        metadata["status"] = "failed"
        metadata["failureReason"] = last_error or "repair_failed"
        return None, metadata

    def _prompt(
        self,
        raw_response: str,
        error: Exception,
        schema_name: str,
        request_context: dict[str, Any],
    ) -> str:
        schema_summary = (
            "AgentStructuredOutput: {reply:string, mode: full_itinerary|patch|clarification|cannot_plan, "
            "fullItinerary?, operations?, poiResolutionRequests?, warnings?}"
            if schema_name == "AgentStructuredOutput"
            else "AgentInitialPlanOutput: {reply:string, mode: day_slots|cannot_plan, daySlots[], intentPools[], warnings?}; "
            "do not return fullItinerary, operations, poiIntents, or searchQueries."
        )
        payload = {
            "instruction": "Repair the model output. Return valid JSON only. Do not explain.",
            "errorType": error.__class__.__name__,
            "parserErrorMessage": str(error),
            "allowedSchemaSummary": schema_summary,
            "rawOutputPreview": str(raw_response or "")[:6000],
            "contextPreview": json.dumps(request_context, ensure_ascii=False, default=str)[:3000],
        }
        return json.dumps(payload, ensure_ascii=False, default=str)
