# Pending POI State Machine

Tags: pending, poi, candidate, 多候选, 低置信, 确认, 忽略, selected, rejected

Use this SOP when POI resolution returns multiple candidates, low confidence, or unresolved results.

- Pending POI candidates are server-owned state, not frontend-only state.
- The user must confirm one AMap candidate before it can be written into the final itinerary.
- Selected candidates should be marked selected with selected_amap_id and then applied through a versioned patch.
- Rejected or expired candidates should not be returned as pending after reload.
- In user-visible replies, say that the itinerary has not been updated until the candidate is confirmed.
