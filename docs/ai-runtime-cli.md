# AI Runtime CLI

Trip backend can be driven without opening the frontend. Use this path when another AI system or automation needs to create a session, send one Agent message, export state, or replay a previous artifact package.

Run from the repository root:

```powershell
.\trip\Scripts\python.exe -m src.cli.agent_cli health --json
.\trip\Scripts\python.exe -m src.cli.agent_cli init-session --city 北京 --json
.\trip\Scripts\python.exe -m src.cli.agent_cli run --input "北京一天，明天出发，1人，预算500，公共交通，轻松一点" --city 北京 --state-dir .ai-runs --json --mock-providers
.\trip\Scripts\python.exe -m src.cli.agent_cli inspect --session-id <sessionId> --json
.\trip\Scripts\python.exe -m src.cli.agent_cli export-state --session-id <sessionId> --output state.json --json
.\trip\Scripts\python.exe -m src.cli.agent_cli replay --artifact .ai-runs\run_<timestamp>_<shortid> --json
```

`baselineRef` is diagnostic metadata and may retain an internal historical label. Use the actual source revision when identifying the version you run.

Installed console script:

```powershell
trip-agent run --input-file input.txt --output final_response.json --json --mock-providers
```

## Command Contract

- `health`: initializes/checks database access, provider configuration, and artifact directory writability. It redacts database URLs and provider configuration.
- `init-session`: creates an Agent session and editable draft day, returning the normal session JSON.
- `run`: creates or opens a session, sends one Agent message through the runtime layer, writes a run artifact directory, and returns `final_response` JSON.
- `inspect`: read-only session inspection for quick status checks.
- `export-state`: read-only full session export for handoff, including turns, versions, patches, planning runs, pending POI candidates, and active itinerary snapshot.
- `replay`: file-only artifact validation. It does not open the database and does not call providers.

## Status And Exit Codes

For automation, the JSON `status` field is authoritative. The process exit code is only a dispatcher-friendly mirror for shell scripts:

| JSON status | Exit code | Meaning |
| --- | ---: | --- |
| `success` | 0 | The run completed and any required write was accepted. |
| `partial_success` | 0 | A write was accepted and verified before a later tool-loop overrun. Keep the returned `activeVersionId`. |
| `needs_confirmation` | 3 | The Agent needs user-provided details or POI confirmation. The CLI does not wait interactively; read `pendingPoiCandidates` and `nextActions`. |
| `validation_failed` | 2 | A patch or verifier-visible write contract failed. The prior `activeVersionId` is preserved and rejected patch details are kept in the artifact. |
| `provider_unavailable` | 4 | Real provider mode was requested but required provider configuration is missing. No fake success data is produced. |
| `stale_version` | 5 | The write used a stale `baseVersionId`; refresh state with `inspect` or `export-state` and retry. |
| `failed` | 1 | The run failed before any verified write, or hit an unexpected runtime/provider error. |

`errors.jsonl` is written for all non-success terminal states above except `needs_confirmation`. Tracebacks are omitted unless debug mode is enabled.

## Provider Modes

Real mode requires `DEEPSEEK_API_KEY` for Agent provider calls. If it is missing, `run` returns `status="provider_unavailable"`, exits with code `4`, writes `errors.jsonl` with stage `provider_preflight`, and does not create a fake itinerary.

`--mock-providers` is for local automation and tests. It does not use real DeepSeek or AMap keys. The runtime mock provider still calls `read_itinerary` and `patch_itinerary`, writes a low-confidence `agent-text-timeline` draft, and does not pretend that POIs are verified AMap places.

## Read-Only Commands

`inspect`, `export-state`, and `replay` must not change turns, versions, planning runs, or active version state. `replay` validates only files under the artifact directory.

## Frontend/Backend Adapter Boundary

Trip has three adapters over the same backend state:

- Frontend is the UI adapter. It renders server responses, sends user actions, and may hold transient UI selection state such as selected tab, selected marker, or pending input text. It must not treat a local draft as the persisted itinerary truth.
- HTTP routes are the browser API adapter. Agent session routes call `TripAgentRuntime`; itinerary writes call `ItineraryPatchService`; deprecated `PATCH /api/itineraries/{planId}` remains only as a compatibility shim and must not synthesize `baseVersionId` for callers.
- CLI is the AI adapter. It uses the same runtime/session/export/replay layer as HTTP Agent routes and writes machine-readable run artifacts.
- `TripAgentRuntime` is the orchestration layer for Agent sessions, messages, inspection, export, replay, and CLI run artifact generation.
- SQLite session/version/patch/pending POI tables are the source of truth for active state. Run artifacts are immutable AI-readable evidence packages for a run.

Boundary rules:

- Active itinerary writes must go through versioned patch APIs and carry `baseVersionId` explicitly. When a session already has an `activeVersionId`, missing or stale `baseVersionId` must fail without changing `activeVersionId`.
- Frontend manual timeline edits, map POI replacements, route selection, and local replan application must replace local itinerary/version/pending state from the server response.
- Pending POI state is backend-authoritative: `pending`, `selected`, `rejected`, and `expired` live in SQLite. After confirm/reject, the frontend uses the returned server session or patch response to overwrite local pending candidates.
- Read-only APIs and commands, including `GET /api/itineraries/{planId}`, `inspect`, `export-state`, and `replay`, must not create planning runs, patches, versions, provider calls, or active-version changes.
