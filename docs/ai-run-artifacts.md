# AI Run Artifacts

Every `trip-agent run` writes one artifact directory:

```text
.ai-runs/run_<timestamp>_<shortid>/
```

The directory is designed for machine handoff. Another AI should be able to inspect what happened, validate the JSON files, and continue from the returned `sessionId`. It is an AI-readable execution package, while SQLite remains the source of truth for active session/version/patch/pending POI state.

## Files

- `manifest.json`: run identity, command, cwd, projectRoot, baselineRef, baselineCommit, gitCommit, redacted database URL, redacted provider status, status, and artifact file map.
- `input.json`: CLI input options after redaction.
- `context.json`: Agent request context stored on the assistant turn.
- `agent_plan.json`: deterministic Agent plan extracted from context.
- `planning_steps.jsonl`: planning events, one JSON object per line.
- `tool_events.jsonl`: tool events, one JSON object per line.
- `patches.jsonl`: itinerary patches visible in the session snapshot, one JSON object per line.
- `final_response.json`: stable command result with status, sessionId, activePlanId, activeVersionId, artifactPath, artifactPathAbsolute, warnings, pendingPoiCandidates, and nextActions.
- `session_snapshot.json`: exported SQLite session state after the run.
- `verifier_report.json`: verifier metadata extracted from the latest verify planning event when available.
- `itinerary_snapshot.json`: active itinerary version snapshot JSON, or null when no active version exists.
- `errors.jsonl`: recoverable runtime errors, one JSON object per line. This file exists even on success.
- `README.md`: local handoff guide for the artifact directory.

## Replay Validation

Use:

```powershell
.\trip\Scripts\python.exe -m src.cli.agent_cli replay --artifact .ai-runs\run_<timestamp>_<shortid> --json
```

Replay checks that required JSON files parse, JSONL files parse line by line, and `final_response.activeVersionId` matches `session_snapshot.conversation_session.active_version_id`.

Replay is intentionally read-only. It validates files under the artifact directory only; it does not open the SQLite database, does not call DeepSeek, and does not call AMap.

## Runtime Statuses

The `final_response.json.status` field is the automation contract:

| Status | Exit code | Meaning |
| --- | ---: | --- |
| `success` | 0 | The run completed and any required write was accepted. |
| `partial_success` | 0 | A write was accepted and verified before a later tool-loop overrun. |
| `needs_confirmation` | 3 | User details or POI confirmation are needed; inspect `pendingPoiCandidates` and `nextActions`. |
| `validation_failed` | 2 | Patch validation or verifier checks failed; prior `activeVersionId` is preserved. |
| `provider_unavailable` | 4 | Real provider mode lacks required configuration; no fake success data is produced. |
| `stale_version` | 5 | A stale `baseVersionId` was used; refresh with `inspect` or `export-state`. |
| `failed` | 1 | The run failed before any verified write, or hit an unexpected runtime/provider error. |

## Handoff Checklist

1. Read `final_response.json`.
2. If `status=success`, use `sessionId` and `activeVersionId` to continue.
3. If `status=needs_confirmation`, inspect `pendingPoiCandidates` and `nextActions`.
4. If `status=failed` or `validation_failed`, read `errors.jsonl`, `tool_events.jsonl`, and `verifier_report.json`.
5. Run `replay` before trusting an artifact copied from another machine.
