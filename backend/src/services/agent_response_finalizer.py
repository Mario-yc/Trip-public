from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.api.schemas.agent import AgentMessageResponse


@dataclass
class AgentResponseFinalizer:
    auto_update_preference_memory: Callable[[str, str], None]
    current_preference_memory: Callable[[], Any]
    current_pending_candidates: Callable[[], list[Any]]
    current_active_state: Callable[[], dict[str, Any]]
    load_itinerary: Callable[[str], Any]
    load_version: Callable[[str], Optional[Any]]
    commit: Callable[[], None]

    def finalize(self, response: Any) -> Any:
        if not isinstance(response, AgentMessageResponse):
            return response
        if response.assistant_turn.status != "failed":
            user_text = response.user_turn.content if response.user_turn else ""
            assistant_text = response.assistant_turn.content if response.assistant_turn else ""
            self.auto_update_preference_memory(user_text, assistant_text)
            self.commit()
        response.preference_memory = self.current_preference_memory()
        response.pending_poi_candidates = self.current_pending_candidates()
        self._refresh_active_version(response)
        return response

    def _refresh_active_version(self, response: AgentMessageResponse) -> None:
        state = self.current_active_state()
        active_plan_id = str(state.get("activePlanId") or "")
        active_version_id = str(state.get("activeVersionId") or "")
        turn_version_id = str(getattr(response.assistant_turn, "itinerary_version_id", "") or "")
        if not active_version_id or response.version is None or not turn_version_id:
            return
        if turn_version_id != active_version_id:
            return
        response_version_id = str(getattr(response.version, "id", "") or "")
        if response_version_id != active_version_id:
            version = self.load_version(active_version_id)
            if version is not None:
                response.version = version
        if response.itinerary is None and active_plan_id:
            response.itinerary = self.load_itinerary(active_plan_id)
