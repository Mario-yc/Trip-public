import json

from pydantic import ValidationError

from src.api.schemas.agent import AgentInitialPlanOutput, AgentStructuredOutput


INVALID_AGENT_JSON_MESSAGE = "Agent returned invalid structured itinerary JSON"


class AgentOutputParser:
    def parse(self, raw_response: str) -> AgentStructuredOutput:
        try:
            normalized = self.normalize(raw_response)
            output = AgentStructuredOutput.model_validate(normalized)
        except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise ValueError(INVALID_AGENT_JSON_MESSAGE) from error
        if output.mode not in {"full_itinerary", "patch", "clarification", "cannot_plan"}:
            raise ValueError(f"Unsupported Agent output mode: {output.mode}")
        if output.mode == "full_itinerary" and output.full_itinerary is None:
            raise ValueError("full_itinerary mode requires fullItinerary")
        if output.mode == "patch" and not output.operations and not output.poi_resolution_requests:
            raise ValueError("patch mode requires operations or POI resolution requests")
        return output

    def parse_initial_plan(self, raw_response: str) -> AgentInitialPlanOutput:
        try:
            normalized = self.normalize_initial_plan(raw_response)
            output = AgentInitialPlanOutput.model_validate(normalized)
        except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise ValueError(INVALID_AGENT_JSON_MESSAGE) from error
        if output.mode not in {"day_slots", "cannot_plan"}:
            raise ValueError(f"Unsupported initial plan output mode: {output.mode}")
        if output.mode == "day_slots" and not output.day_slots:
            raise ValueError("day_slots mode requires daySlots")
        return output

    def normalize(self, raw_response: str) -> dict:
        payload = json.loads(self._extract_json_object(raw_response))
        if not isinstance(payload, dict):
            raise ValueError("Agent output root must be a JSON object")

        full_itinerary = payload.get("fullItinerary")
        if isinstance(full_itinerary, dict):
            self._copy_first_present(full_itinerary, "title", ["tripTitle", "itineraryTitle"])
            for day in full_itinerary.get("days") or []:
                if not isinstance(day, dict):
                    continue
                self._copy_first_present(day, "title", ["dayTitle"])
                for segment in day.get("segments") or []:
                    if not isinstance(segment, dict):
                        continue
                    self._copy_first_present(segment, "durationMinutes", ["duration"])
                    self._copy_first_present(segment, "startTime", ["start"])

        requests = payload.get("poiResolutionRequests")
        if isinstance(requests, list):
            for request in requests:
                if not isinstance(request, dict):
                    continue
                self._copy_first_present(request, "name", ["poiName", "keyword"])
                self._copy_first_present(request, "category", ["type"])

        operations = payload.get("operations")
        if isinstance(operations, list):
            for operation in operations:
                if not isinstance(operation, dict):
                    continue
                self._copy_first_present(operation, "startTime", ["start"])
                self._copy_first_present(operation, "durationMinutes", ["duration"])
        return payload

    def normalize_initial_plan(self, raw_response: str) -> dict:
        payload = json.loads(self._extract_json_object(raw_response))
        if not isinstance(payload, dict):
            raise ValueError("Agent initial plan output root must be a JSON object")
        if "fullItinerary" in payload:
            raise ValueError("initial day-slot planner must not return fullItinerary")
        if "operations" in payload:
            raise ValueError("initial day-slot planner must not return operations")
        if "poiIntents" in payload or "poi_intents" in payload:
            raise ValueError("initial day-slot planner must not return poiIntents")
        day_slots = payload.get("daySlots")
        if isinstance(day_slots, list):
            for slot in day_slots:
                if not isinstance(slot, dict):
                    continue
                self._copy_first_present(slot, "dayNumber", ["day"])
                self._copy_first_present(slot, "timeWindow", ["window"])
                self._copy_first_present(slot, "startTime", ["start"])
                self._copy_first_present(slot, "durationMinutes", ["duration"])
                self._copy_first_present(slot, "slotId", ["id", "slot_id"])
                self._copy_first_present(slot, "rawNeed", ["need"])
                self._copy_first_present(slot, "routeAnchor", ["route_anchor"])
        intent_pools = payload.get("intentPools")
        if isinstance(intent_pools, list):
            for pool in intent_pools:
                if not isinstance(pool, dict):
                    continue
                self._copy_first_present(pool, "poolId", ["id", "pool_id"])
                self._copy_first_present(pool, "rawNeed", ["need"])
                self._copy_first_present(pool, "intentType", ["type"])
                self._copy_first_present(pool, "targetCount", ["count", "target"])
                self._copy_first_present(pool, "assignToSlots", ["slots", "slotIds"])
                self._copy_first_present(pool, "candidateHints", ["hints", "candidate_hints"])
                self._copy_first_present(pool, "hintPolicy", ["hint_policy"])
                if "searchQueries" in pool or "search_queries" in pool:
                    raise ValueError("initial IntentPool must not return searchQueries")
        return payload

    def _extract_json_object(self, raw_response: str) -> str:
        raw_response = raw_response.strip()
        if raw_response.startswith("{") and raw_response.endswith("}"):
            return raw_response
        start = raw_response.find("{")
        end = raw_response.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Agent output was not valid JSON")
        return raw_response[start : end + 1]

    def _copy_first_present(self, payload: dict, target_key: str, alias_keys: list[str]) -> None:
        target_present = payload.get(target_key) not in (None, "")
        for alias_key in alias_keys:
            alias_value = payload.pop(alias_key, None)
            if not target_present and alias_value not in (None, ""):
                payload[target_key] = alias_value
                target_present = True
