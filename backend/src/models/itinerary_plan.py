from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class ItineraryPlan:
    id: str
    user_id: str
    inspiration_set_id: str
    title: str
    city: str
    template_type: str = "custom"
    budget_target: Optional[float] = None
    budget_tier: str = "unknown"
    budget_estimate: float = 0.0
    budget_delta_explanation: str = "预算为软约束，当前为初版估算。"
    decision_rationale: str = "按识别 POI 生成空间顺序，并叠加路线、天气和拥挤风险。"
    status: str = "draft"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
