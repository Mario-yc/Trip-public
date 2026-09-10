import sqlite3

from src.api.schemas.maps import MapPoiResponse
from src.services.agent_service import AgentService
from src.services.map_poi_service import AMAP_PLACE_SOURCE


def test_exact_guomao_policy_rejects_shichahai_viewpoint_candidate():
    service = AgentService(sqlite3.connect(":memory:"), provider=object())
    indirect = MapPoiResponse(
        id="B0INDIRECT1",
        name="什刹海-遥望国贸CBD(打卡点)",
        type="风景名胜;观景点",
        city="北京",
        district="西城区",
        address="什刹海",
        longitude=116.38,
        latitude=39.94,
        category="scenic",
        source=AMAP_PLACE_SOURCE,
        sourceNote="test",
        confidence=0.95,
    )
    direct = MapPoiResponse(
        id="B0DIRECT001",
        name="国贸商城",
        type="购物服务;购物中心;商圈",
        city="北京",
        district="朝阳区",
        address="建国门外大街1号",
        longitude=116.46,
        latitude=39.91,
        category="scenic",
        source=AMAP_PLACE_SOURCE,
        sourceNote="test",
        confidence=0.9,
    )

    assert service._valid_local_night_view_candidate(indirect, "国贸") is False
    assert service._valid_local_night_view_candidate(direct, "国贸") is True
