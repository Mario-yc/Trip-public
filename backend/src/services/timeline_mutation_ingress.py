from __future__ import annotations

from typing import Optional

from src.services.timeline_mutation_intent_service import TimelineMutationIntentExtractor
from src.services.timeline_mutation_models import TimelineMutationIntent


class TimelineMutationIngress:
    def __init__(self, extractor: Optional[TimelineMutationIntentExtractor] = None):
        self.extractor = extractor or TimelineMutationIntentExtractor()

    def detect(self, text: str, *, active_version_id: Optional[str]) -> Optional[TimelineMutationIntent]:
        intent = self.extractor.extract(text, has_active_timeline=bool(active_version_id))
        if intent is None or intent.confidence < 0.9:
            return None
        return intent
