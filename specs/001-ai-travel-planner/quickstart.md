# Quickstart: AI Travel Planner MVP

## Goal

Validate the MVP closed loop:

```text
Agent conversation -> DeepSeek structured itinerary -> AMap-grounded POIs
-> persisted timeline edits -> conversation edit rollback -> regenerated branch
```

Comparison and inspiration upload remain available as auxiliary flows, but they do not drive the active Agent itinerary state.

## Prerequisites

- Python runtime in `.\trip\Scripts\python.exe`.
- Node.js and npm for the frontend.
- DeepSeek API key for real Agent smoke.
- AMap WebService key for POI grounding.
- AMap JS key and security code for the browser map surface.

## Environment

Copy `backend/.env.example` to `backend/.env`.

Minimum real-demo configuration:

```text
DEEPSEEK_API_KEY=your_deepseek_key
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_TIMEOUT_SECONDS=30
MAP_PROVIDER_KEY=your_amap_webservice_key
MAP_JS_API_KEY=your_amap_js_key
MAP_PROVIDER_SECURITY_JS_CODE=your_amap_security_code
DATABASE_URL=sqlite:///./trip_demo.db
DEFAULT_USER_ID=local-demo-user
PROVIDER_MODE=mock
FRONTEND_ORIGIN=http://localhost:5173
```

Do not commit real keys. Default tests mock DeepSeek and AMap HTTP.

## Local Setup

Install backend dependencies:

```powershell
cd Trip-public
uv pip install --python .\trip\Scripts\python.exe -e "backend[dev]"
```

Run backend tests:

```powershell
cd Trip-public
.\trip\Scripts\python.exe -m pytest backend/tests -q --tb=short -p no:cacheprovider
.\trip\Scripts\python.exe -m ruff check backend/src backend/tests
.\trip\Scripts\python.exe backend/evals/run_offline.py
```

Start backend:

```powershell
cd Trip-public
.\trip\Scripts\python.exe -m uvicorn src.main:app --app-dir backend --reload
```

Install and start frontend:

```powershell
cd Trip-public\frontend
npm install
npm run dev
```

Verify frontend:

```powershell
cd Trip-public\frontend
npm test
npm run build
npm run lint
npm run format:check
```

Run the full local gate in the same order as CI:

```powershell
cd Trip-public
.\scripts\verify.ps1
```

## MVP Smoke Flow

1. Open the frontend in a desktop browser.
2. Confirm `/api/providers/status` returns:
   - `agent.providerName = "DeepSeek"`
   - `agent.configured = true` when `DEEPSEEK_API_KEY` is set
   - `agent.model` equals `DEEPSEEK_MODEL`
   - no API key value appears in the response
3. In the left Agent input, send:

```text
帮我安排北京两天，轻松一点，想去故宫和胡同
```

4. Confirm the right timeline is generated in one request-response cycle and persisted to SQLite.
5. Confirm final itinerary segment POIs have:
   - `amapId`
   - real `longitude` and `latitude`
   - source `amap-place-search`
   - confidence `>= 0.8`
6. Manually edit a segment start time in the right timeline.
7. Reload the session and confirm the manual edit remains.
8. Send:

```text
把故宫放到下午
```

9. Confirm the timeline updates through a validated patch and creates a new itinerary version.
10. Send:

```text
加一个附近餐厅
```

11. If AMap returns multiple candidates or low confidence, confirm the UI asks for confirmation and no fake POI is inserted.
12. Edit the first user message.
13. Confirm later turns are marked `superseded`, the itinerary rolls back to that message's snapshot, and a new assistant turn/version is created.
14. Mock or force an invalid Agent patch and confirm it is rejected without changing `activeVersionId` or active itinerary.

## Persistence Invariants

- Active Agent itinerary writes use `POST /api/itineraries/{plan_id}/patch` or versioned route-select endpoints.
- Every accepted active itinerary write creates an `itinerary_patches` row and an `itinerary_versions` row.
- Every write from the UI after the first active version includes `baseVersionId`; stale values return `409` and must not update `activeVersionId`.
- `PATCH /api/itineraries/{plan_id}` is retained only as a deprecated compatibility route and internally delegates to versioned patch.
- Pending POI state is persisted in `amap_poi_candidates`: `pending -> selected` when a resolved AMap POI is confirmed by patch, `pending -> rejected` when the user ignores it, and `pending -> expired` when a new Agent turn supersedes unresolved candidates.

## Agent Harness P0 Offline Eval

Run the offline eval after backend unit/contract tests:

```powershell
cd Trip-public
.\trip\Scripts\python.exe backend/evals/run_offline.py
```

The runner creates a temporary SQLite database, uses a mock Agent provider, mocks AMap place search, and never calls real DeepSeek or AMap. The JSON summary contains:

- `total`, `passed`, `failed`, `passRate`, `passAtK`, `repeat`
- `failures`
- `avgSteps`, `p95LatencyMs`
- `invalidPatchCount`
- `verifierFailures`, `toolCallCount`, `failedToolCallCount`
- `traceReplayFailures`, `stageCoverage`

Covered cases:

- `happy_path_beijing_1_day`
- `pending_poi_multiple_candidates`
- `manual_patch_stale_version`
- `edit_message_rollback`
- `invalid_agent_patch`
- `ticket_fallback_transparency`
- `tool_loop_read_patch_verify`

Harness P0 includes trace events, verifier checks, curated skills/SOP retrieval, and this offline eval runner. P1/P2 work is deferred: best-of-k, long-term hidden memory, tree-of-thought search, and OTLP tracing.

Phase 1 additions:

- Eval cases are data-driven JSON fixtures with ordered `steps`, mock Agent/tool-loop payloads, mock AMap responses, assertions, and replay stage requirements.
- `backend/evals/run_offline.py --output <file>` saves a replayable eval record.
- `backend/evals/replay_trace.py --input <file>` replays the saved trace reports.
- `AgentPlannerService` adds a deterministic `agentPlan` with measurable plan items to Agent context before DeepSeek/mock provider execution.

Example:

```powershell
cd Trip-public
.\trip\Scripts\python.exe backend/evals/run_offline.py --output .\backend\evals\last-run.json
.\trip\Scripts\python.exe backend/evals/replay_trace.py --input .\backend\evals\last-run.json
```

## Auxiliary Checks

- Add a guide link or image file; confirm the old inspiration extraction flow still runs as supporting material.
- Open the comparison tab; confirm it is clearly auxiliary or unavailable and does not replace the active Agent itinerary.
- Remove `DEEPSEEK_API_KEY`; confirm provider status shows DeepSeek unavailable and local tests still pass.
- Remove `MAP_PROVIDER_KEY`; confirm POI insertion/resolve paths return a clear provider error instead of inserting fallback POIs.

## Non-Goals For This MVP

- Multi-branch visual history.
- Account system and cross-device sync.
- Real booking, payment, export, or calendar integration.
- Full real ticket/weather/traffic provider coverage.
- Mobile optimization.
