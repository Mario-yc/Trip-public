from uuid import uuid4

from src.models.route_option import RouteOption
from src.models.traffic_crowding_signal import TrafficCrowdingSignal


class TrafficService:
    def build_signals(self, route_options: list[RouteOption]) -> list[TrafficCrowdingSignal]:
        signals = []
        for route in route_options:
            crowding_level = "unknown"
            signals.append(
                TrafficCrowdingSignal(
                    id=f"traffic_{uuid4().hex[:10]}",
                    route_option_id=route.id,
                    real_data_available=False,
                    crowding_level=crowding_level,
                    estimated_reason="暂无实时拥挤数据，不能据此判断拥挤程度",
                    recommended_departure_adjustment="请在临近出发时查看实时交通",
                )
            )
        return signals
