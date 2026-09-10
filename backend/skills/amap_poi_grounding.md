# AMap POI Grounding

Tags: amap, poi, 高德, 景点, 地图, 坐标, 餐厅, 博物馆

Use this SOP whenever the Agent proposes or edits itinerary places.

- 高德 AMap 是 MVP 最终 POI 的权威 grounding 来源。
- Final itinerary POIs must come from AMap WebService results or a user-selected AMap POI.
- Never invent coordinates, reuse city fallback coordinates, or persist mock-map-provider POIs.
- If the user names a place, request POI resolution before writing itinerary segments.
- Accepted POIs need amapId, name, longitude, latitude, source, confidence, city/district/address when available.
- Ambiguous, low-confidence, or failed POI resolution must stay pending and must not enter final itinerary segments.
