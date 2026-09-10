# API Contract: AI Travel Planner Agent Demo

This contract documents the first-release Web app API surface. Endpoint names are implementation-facing, but responses preserve user-visible requirements from the specification.

## Common Response Types

### ProviderMeta

```json
{
  "providerKind": "ticket",
  "providerName": "default-ticket-provider",
  "isMock": false,
  "sourceName": "Official or aggregation source",
  "sourceUrl": "https://example.com",
  "queriedAt": "2026-05-31T10:00:00+08:00",
  "confidence": 0.82,
  "credibilityRank": "official",
  "userVisibleCaveat": "查询结果仅供参考，请以购票平台为准"
}
```

### ErrorResponse

```json
{
  "errorCode": "PROVIDER_UNAVAILABLE",
  "message": "User-facing explanation",
  "recoverable": true,
  "fallbackUsed": true
}
```

### AgentPlanningEvent

Agent message/edit responses expose the local Harness P0 trace in both `planningSteps` and `toolEvents`.

```json
{
  "type": "apply_patch",
  "label": "Versioned itinerary patch",
  "status": "completed",
  "detail": "Applied accepted versioned patch.",
  "sessionId": "sess_001",
  "turnId": "turn_002",
  "providerName": "sqlite",
  "toolProvider": "itinerary_patch_service",
  "fallbackUsed": false,
  "failureReason": null,
  "durationMs": 0,
  "metadata": {
    "versionId": "ver_001"
  },
  "timestamp": "2026-06-17T10:00:00+00:00"
}
```

Required Harness P0 stage types for Agent message/edit flows:

- `plan`: Agent context was built from active turns, timeline, preference memory, and pending POIs.
- `resolve_poi`: AMap POI grounding ran or verified that no new POI resolution was needed.
- `apply_patch`: itinerary changes went through the versioned patch service or no write was accepted.
- `verify`: rule verifier checked POI grounding, pending POI leakage, patch/version invariants, source fallback transparency, or active-version stability.
- `respond`: assistant response and trace events were persisted.

### Agent Harness P0 Offline Eval Summary

`backend/evals/run_offline.py` is a local eval runner, not a product API. It uses mock Agent and mock AMap providers and must not require real DeepSeek, AMap, web-search, ticket, or weather keys.

Output:

```json
{
  "summary": {
    "total": 7,
    "passed": 7,
    "failed": 0,
    "passRate": 1.0,
    "passAtK": 1.0,
    "repeat": 1,
    "failures": [],
    "avgSteps": 9.5,
    "p95LatencyMs": 120,
    "invalidPatchCount": 2,
    "verifierFailures": 0,
    "toolCallCount": 2,
    "failedToolCallCount": 0,
    "traceReplayFailures": 0
  },
  "cases": [
    {
      "id": "happy_path_beijing_1_day",
      "passed": true,
      "failureReason": "",
      "stepCount": 14,
      "invalidPatchCount": 0,
      "verifierFailures": 0,
      "toolCallCount": 0,
      "failedToolCallCount": 0
    }
  ]
}
```

P0 coverage:

- `happy_path_beijing_1_day`
- `pending_poi_multiple_candidates`
- `manual_patch_stale_version`
- `edit_message_rollback`
- `invalid_agent_patch`
- `ticket_fallback_transparency`
- `tool_loop_read_patch_verify`

Harness P0 currently means trace + verifier + curated skills/SOP retrieval + offline eval. P1/P2 capabilities are explicitly deferred: best-of-k, long-term hidden memory, tree-of-thought search, and OTLP/distributed tracing.

Phase 1 eval/replay contract:

- `backend/evals/cases/*.json` is the source of truth for offline eval case data.
- Each case declares ordered `steps`, mock provider data, `mockAmapResponses`, JSON-path `assertions`, and `expect`.
- Tool-loop cases use `providerMode: "tool_loop"` and `toolLoopScript` to exercise registered tools without a real DeepSeek call.
- `expect.replayStages` controls which harness stages are replay-required. An empty list means the case is an API/invariant eval without harness trace replay.
- `expect.allowedFailedStages` is only for cases where a failed harness stage is the expected safe behavior, such as invalid Agent patch rejection.
- Runner output includes `traceReplay.events`, so saved JSON can be replayed offline without rerunning providers.

Saved output can be replayed:

```powershell
.\trip\Scripts\python.exe backend/evals/run_offline.py --output .\backend\evals\last-run.json
.\trip\Scripts\python.exe backend/evals/replay_trace.py --input .\backend\evals\last-run.json
```

Agent request context now includes an explicit deterministic planner block:

```json
{
  "agentPlan": {
    "plannerVersion": "explicit-planner-p1-v1",
    "intent": "draft_itinerary",
    "writeIntent": "versioned_patch",
    "requiresPoiResolution": true,
    "requiresPatch": true,
    "requiresRollback": false,
    "expectedHarnessStages": ["plan", "resolve_poi", "apply_patch", "verify", "respond"],
    "riskControls": ["patch_before_write", "active_version_verifier", "amap_poi_grounding"],
    "items": [
      {
        "goal": "Ground all final POIs through AMap before itinerary write.",
        "successCriteria": "Every persisted segment POI has amapId, coordinates, source, and confidence.",
        "requiresTool": "poi_resolution_service"
      }
    ]
  }
}
```

### WeatherSignal

```json
{
  "id": "weather_001",
  "city": "北京",
  "date": "2026-06-10",
  "hourlyForecast": [],
  "dailySummary": "待 Agent 查询",
  "riskLevel": "unavailable",
  "purposeImpactReason": "天气服务暂不可用，当前天气风险判断不完整。",
  "source": "天气服务",
  "dataStatus": "unavailable",
  "confidence": 0,
  "failureReason": "天气服务未配置，无法查询真实天气。",
  "sourceUrl": null,
  "userVisibleCaveat": "待 Agent 在配置天气服务后联网查询天气；当前天气风险判断不完整。",
  "queriedAt": "2026-06-10T10:00:00+08:00"
}
```

When a real weather provider is configured, `source` is user-facing (for example `高德天气`), `dataStatus` may be `available` or `degraded`, and `failureReason` is null. The first implementation may return day-level forecast rows inside `hourlyForecast` with `dataStatus=degraded` until true hourly data is connected.

### POIRiskAlert

```json
{
  "id": "risk_001",
  "planId": "plan_001",
  "segmentId": "seg_001",
  "poiName": "故宫博物院",
  "status": "unavailable",
  "summary": "未完成近期公开信息搜索，景点风险判断不完整。",
  "sourceName": "搜索结果",
  "sourceUrl": null,
  "sources": [],
  "confidence": 0,
  "failureReason": "搜索服务未配置，无法联网搜索近期景点风险信息。",
  "userVisibleCaveat": "无法联网搜索近期景点信息，当前风险判断不完整；请在出行前核对景区官方公告、预约状态和交通管制。",
  "queriedAt": "2026-06-10T10:00:00+08:00"
}
```

When search is configured, `sources` contains public result title/url/snippet entries and `sourceUrl` points to the top source. When search succeeds but Agent risk synthesis is unavailable, the response MUST return `status=degraded`, keep the public-source summary visible, and explain that the risk judgment is incomplete. When search is unavailable or fails, the response MUST keep the itinerary visible and return `status=unavailable` with `failureReason`; it MUST NOT fabricate risk findings.

## Endpoints

### POST /api/inspirations

Create an inspiration set from text, links, or uploaded material references.

Request:

```json
{
  "cityHint": "北京",
  "textItems": ["攻略正文"],
  "socialLinks": ["https://example.com/note"],
  "sourceMaterialIds": ["mat_001"],
  "saveOriginalImages": false
}
```

Response:

```json
{
  "inspirationSetId": "insp_001",
  "status": "extracting"
}
```

### POST /api/source-materials

Upload screenshot/photo material and create thumbnail plus extraction candidate.

Response:

```json
{
  "sourceMaterialId": "mat_001",
  "thumbnailUrl": "/media/thumbs/mat_001.jpg",
  "originalRetention": "temporary_cache"
}
```

### POST /api/inspirations/{id}/extract

Run multimodal extraction and return structured clues.

Response:

```json
{
  "inspirationSetId": "insp_001",
  "cityCandidates": ["北京"],
  "poiCandidates": [
    {
      "name": "故宫博物院",
      "confidence": 0.91,
      "sourceLinks": ["https://example.com/source"]
    }
  ],
  "styleTags": ["拍照", "历史文化"],
  "budgetClues": ["3000 左右"],
  "routeClues": ["市中心一天"],
  "needsUserConfirmation": false
}
```

### POST /api/itineraries/generate

Generate the three fixed comparison templates.

Request:

```json
{
  "inspirationSetId": "insp_001",
  "city": "北京",
  "dateRange": {
    "start": "2026-06-10",
    "end": "2026-06-12"
  },
  "preferenceProfileId": "pref_001"
}
```

Response:

```json
{
  "plans": [
    {
      "planId": "plan_low",
      "templateType": "low_budget",
      "title": "低预算方案",
      "budgetEstimate": 2860,
      "budgetDeltaExplanation": "接近预算，无需强制重排",
      "decisionRationale": "减少跨城交通和高价景点"
    }
  ]
}
```

### GET /api/itineraries/{id}

Return saved itinerary without side effects. This endpoint MUST NOT create a planning run or refresh ticket status.
Use `POST /api/itineraries/{id}/tickets/refresh` for explicit ticket refresh.

Response includes:

- itinerary days and segments
- map POIs and route options
- ticket lookup results with provider meta from the latest explicit generation or refresh
- weather and crowding signals
- POI risk alerts with search source links or explicit incomplete-search failure
- preference summary card

### POST /api/itineraries/{id}/patch

Apply user edits, Agent changes, and map POI confirmations through a versioned patch. All active itinerary writes MUST create an `itinerary_patches` row and a resulting `itinerary_versions` snapshot. After the first active version exists, clients MUST send `baseVersionId`; stale versions return `409` and MUST NOT change `activeVersionId`.

Request:

```json
{
  "sourceType": "manual",
  "baseVersionId": "ver_001",
  "planningContext": {
    "pendingPoiCandidateId": "cand_001"
  },
  "operations": [
    {
      "op": "add_segment",
      "dayId": "day_001",
      "startTime": "15:00",
      "durationMinutes": 60,
      "amapPoi": {
        "id": "B000A8UIN8",
        "name": "故宫博物院",
        "source": "amap-place-search",
        "confidence": 0.91,
        "longitude": 116.397026,
        "latitude": 39.918058
      }
    }
  ]
}
```

Response:

```json
{
  "itinerary": {},
  "patch": {
    "id": "patch_001",
    "validationStatus": "accepted"
  },
  "version": {
    "id": "ver_002",
    "versionNumber": 2,
    "sourceType": "manual"
  },
  "validationErrors": [],
  "planningRun": null,
  "pendingPoiCandidates": []
}
```

Validation failures return a structured error with `code=PATCH_VALIDATION_FAILED` and `validationErrors` so the UI can show a friendly message without mutating local itinerary state.


### PATCH /api/itineraries/{id}

Deprecated compatibility path for legacy transport-mode edits only. New code MUST use `POST /api/itineraries/{id}/patch`. The compatibility route returns `Deprecation: true`, delegates internally to the versioned patch service, and still creates patch/version audit rows.

The compatibility route MUST NOT synthesize `baseVersionId` from server session state. If an active version already exists, callers must provide `baseVersionId`; missing or stale values return `409` and MUST NOT change `activeVersionId`.

### POST /api/agent/sessions/{sessionId}/pending-poi-candidates/{candidateId}/reject

Persist rejection of a pending POI candidate. Reloading the Agent session MUST no longer return rejected candidates.

Pending POI state machine:

- `pending`: created by POI resolve when no unique high-confidence AMap match exists.
- `selected`: set by versioned patch when a resolved AMap POI is added or used to replace a segment POI; `selectedAmapId` is persisted.
- `rejected`: set when the user ignores a candidate.
- `expired`: set when a new Agent turn supersedes old unresolved candidates.

### GET /api/providers/status

Return default and mock provider health for LLM, vision, map, ticket, weather, traffic, search, and email.

### POST /api/preferences/extract

Extract preferences from a conversation snippet.

Response:

```json
{
  "summaryCard": {
    "partySize": 3,
    "travelerTypes": ["adult", "elder"],
    "budgetRange": "3000 左右",
    "pacePreference": "轻松不赶路",
    "items": [
      {
        "label": "拍照优先",
        "sourceText": "想多拍照"
      }
    ]
  }
}
```

### PATCH /api/preferences/{id}

Update confirmed preference summary card contents.

### POST /api/reminders/weather

Create a simulated same-day weather email reminder.

Request:

```json
{
  "itineraryPlanId": "plan_001",
  "emailAddress": "demo@example.com",
  "triggerDate": "2026-06-10"
}
```

Response:

```json
{
  "reminderId": "rem_001",
  "simulatedStatus": "scheduled",
  "subject": "天气风险提醒",
  "bodyPreview": "当天可能下雨，会影响拍照行程。"
}
```

### POST /api/source-materials/cleanup-originals

Manually clear temporary original-image cache while retaining thumbnails and structured results.

Response:

```json
{
  "clearedCount": 4,
  "retainedLongTermCount": 1
}
```
