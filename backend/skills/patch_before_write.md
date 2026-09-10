# Patch Before Write

Tags: patch, version, itinerary, 行程, 修改, 时间, 添加, 删除, 移动, 标题

Use this SOP for any itinerary creation or edit.

- The Agent plans changes, but only the backend patch service writes itinerary state.
- Use supported patch operations only; do not describe database writes as already completed until the patch is accepted.
- Every itinerary write must carry the current baseVersionId and create a new patch/version snapshot.
- If a requested edit conflicts with time order, segment/day ids, or POI grounding, ask for clarification or return a safe failure.
- Do not bypass itinerary_patch_service for manual edits, Agent edits, or POI insertion.
