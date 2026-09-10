"""Portable recorded evidence for the complete dual-portfolio adoption journey."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.runtime.run_artifacts import (
    LOCAL_ABSOLUTE_PATH_RE,
    SECRET_VALUE_RE,
    RunArtifactReplayer,
    RunArtifactWriter,
    is_secret_key,
    redact_artifact,
)
from src.services.creative_planning_models import (
    canonical_fingerprint,
    proposal_canonical_signature,
)


RUN_LABELS = ("turn_1", "turn_2", "turn_3_duplicate")
SCORE_EVIDENCE_FIELDS = {
    "densityDecisionSource",
    "preferenceFit",
    "thematicCoherence",
    "experienceDiversity",
    "routeEfficiency",
    "pacingQuality",
    "novelty",
    "robustness",
    "uncertaintyPenalty",
    "estimatedCostCny",
}
FORBIDDEN_REASONING_KEYS = {
    "chainofthought",
    "hiddenreasoning",
    "rawreasoning",
    "privatereasoning",
}


def workspace_evidence_fingerprints(repo_root: Path) -> dict[str, str]:
    """Freeze the reviewed base plus dirty-worktree evidence without writing Git."""

    def git_output(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.replace("\r\n", "\n")

    head = git_output("rev-parse", "HEAD").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("dual_artifact_git_commit_unavailable")
    tracked_diff = git_output("diff", "--binary", "HEAD", "--")
    status = git_output("status", "--short", "--untracked-files=all")
    return {
        "gitCommit": head,
        "trackedDiffSha256": hashlib.sha256(tracked_diff.encode("utf-8")).hexdigest(),
        "statusSha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def write_dual_adoption_artifact_bundle(
    *,
    artifact_root: Path,
    repo_root: Path,
    session_id: str,
    portfolio_id: str,
    selected_proposal_id: str,
    turns: list[dict[str, Any]],
    export_state: dict[str, Any],
) -> dict[str, Any]:
    """Write three complete RunArtifact directories from captured server state.

    The caller captures state immediately after offer, commit, and duplicate
    replay.  This function never calls a provider and labels that fact in every
    manifest instead of presenting recorded evidence as a live model run.
    """

    if [str(item.get("label") or "") for item in turns] != list(RUN_LABELS):
        raise ValueError("dual_artifact_turn_order_invalid")
    artifact_root.mkdir(parents=True, exist_ok=False)
    fingerprints = workspace_evidence_fingerprints(repo_root)
    bundle_sequence = 0
    run_records: list[dict[str, Any]] = []
    replay_records: dict[str, Any] = {}
    previous_state: dict[str, Any] | None = None

    for turn in turns:
        label = str(turn["label"])
        state = dict(turn.get("state") or {})
        response = dict(turn.get("response") or {})
        local_events = _recorded_turn_events(
            label=label,
            state=state,
            response=response,
            portfolio_id=portfolio_id,
        )
        events, bundle_sequence = _normalize_events(
            local_events,
            label=label,
            starting_bundle_sequence=bundle_sequence,
            state=state,
            response=response,
        )
        run_state_dir = artifact_root / "runs" / label
        run_state_dir.mkdir(parents=True, exist_ok=True)
        writer = RunArtifactWriter(run_state_dir, run_id=f"run_dual_{label}")
        writer.ensure_jsonl_files()

        portfolio = _portfolio_from_state(state, portfolio_id)
        proposals = _proposals_from_state(state, portfolio_id)
        active_snapshot = state.get("active_itinerary_snapshot")
        active_snapshot_fingerprint = (
            canonical_fingerprint(active_snapshot) if isinstance(active_snapshot, dict) and active_snapshot else None
        )
        selected_id = str(portfolio.get("selected_proposal_id") or "") or None
        deltas = _state_deltas(previous_state, state)
        expected_deltas = list(turn.get("expectedDeltas") or [])
        if expected_deltas and deltas != expected_deltas:
            raise ValueError(f"dual_artifact_state_delta_mismatch:{label}:{deltas}:{expected_deltas}")
        assistant_reply = _assistant_reply(response)
        final_response = {
            **response,
            "schemaVersion": "creative-portfolio-dual-turn-response-v1",
            "sessionId": session_id,
            "portfolioId": portfolio_id,
            "selectedProposalId": selected_id,
            "assistantReply": assistant_reply,
            "activeVersionId": ((state.get("conversation_session") or {}).get("active_version_id")),
            "selectedSnapshotFingerprint": active_snapshot_fingerprint,
            "versionDelta": deltas[0],
            "patchDelta": deltas[1],
            "routeWriteDelta": deltas[2],
            "artifactPath": f"artifact://{writer.run_id}",
        }
        selection = {
            "schemaVersion": "creative-portfolio-dual-selection-v1",
            "portfolioId": portfolio_id,
            "visibleProposalIds": list((portfolio.get("summary_json") or {}).get("visibleProposalIds") or []),
            "selectedProposalId": selected_id,
            "activeVersionId": final_response["activeVersionId"],
            "selectedSnapshotFingerprint": active_snapshot_fingerprint,
            "versionDelta": deltas[0],
            "patchDelta": deltas[1],
            "routeWriteDelta": deltas[2],
            "duplicate": label == "turn_3_duplicate",
        }
        started_at = str(events[0]["timestamp"])
        finished_at = str(events[-1]["timestamp"])
        manifest = {
            "schemaVersion": "trip-ai-runtime-artifact-v1",
            "runId": writer.run_id,
            "sessionId": session_id,
            "startedAt": started_at,
            "finishedAt": finished_at,
            "status": str(final_response.get("terminalStatus") or "success"),
            "command": ["trip-agent", "recorded-dual-golden", label],
            "cwd": ".",
            "projectRoot": "trip",
            "baselineRef": "dirty-worktree-evidence",
            "baselineCommit": fingerprints["gitCommit"],
            **fingerprints,
            "uncommittedEvidence": True,
            "databaseUrl": "sqlite:///[redacted]/dual-adoption-golden.sqlite",
            "providerStatus": {
                "mode": "recorded",
                "agent": {
                    "provider": "recorded-deterministic-fixture",
                    "configuredModelId": None,
                    "modelConfigRef": None,
                    "liveModelStatus": "NOT_RUN(recorded deterministic fixture)",
                },
                "amap": {
                    "provider": "amap-webservice",
                    "mode": "recorded",
                    "fixture": "deterministic-positive-route-matrix",
                },
            },
            "artifactFiles": writer.artifact_files(),
        }
        writer.write_json("manifest", manifest)
        writer.write_json(
            "input",
            {
                "input": str(turn.get("input") or ""),
                "selectedAgentChoice": turn.get("selectedAgentChoice"),
            },
        )
        writer.write_json("context", turn.get("context") or {})
        writer.write_json(
            "agentPlan",
            turn.get("agentPlan")
            or response.get("initialPlan")
            or {"action": "select_plan_proposal", "selectedProposalId": selected_id},
        )
        writer.write_json(
            "agentDecision",
            turn.get("agentDecision")
            or {
                "action": "ask_user" if label == "turn_1" else "select_plan_proposal",
                "selectedProposalId": selected_id,
            },
        )
        writer.write_json(
            "agentStop",
            {
                "terminalStatus": final_response.get("terminalStatus"),
                "activeVersionId": final_response.get("activeVersionId"),
                "selectedProposalId": selected_id,
            },
        )
        writer.append_jsonl("planningSteps", events)
        writer.append_jsonl("toolEvents", events)
        writer.append_jsonl(
            "patches",
            _new_rows(
                previous_state,
                state,
                key="itinerary_patches",
                identity_key="id",
            ),
        )
        writer.append_jsonl(
            "agentObservations",
            [
                {
                    "schemaVersion": "recorded-agent-observation-v1",
                    "turn": label,
                    "portfolioId": portfolio_id,
                    "proposalCount": len(proposals),
                    "activeVersionId": final_response["activeVersionId"],
                }
            ],
        )
        writer.append_jsonl(
            "agentDecisions",
            [
                {
                    "schemaVersion": "recorded-agent-decision-v1",
                    "turn": label,
                    "action": "ask_user" if label == "turn_1" else "select_plan_proposal",
                    "selectedProposalId": selected_id,
                }
            ],
        )
        writer.append_jsonl(
            "agentActionOutcomes",
            [
                {
                    "schemaVersion": "recorded-agent-action-outcome-v1",
                    "turn": label,
                    "versionPatchRouteDelta": deltas,
                    "activeVersionId": final_response["activeVersionId"],
                }
            ],
        )
        writer.write_json("portfolio", portfolio)
        writer.append_jsonl("planProposals", proposals)
        writer.append_jsonl(
            "portfolioScores",
            [
                {
                    "proposalId": item.get("id"),
                    "canonicalSignature": item.get("canonical_signature"),
                    "score": item.get("score_json") or {},
                    "generationLineage": item.get("generation_lineage_json") or {},
                }
                for item in proposals
            ],
        )
        writer.append_jsonl(
            "portfolioVerifier",
            [
                {
                    "proposalId": item.get("id"),
                    "verifier": item.get("verifier_json") or {},
                }
                for item in proposals
            ],
        )
        writer.write_json("portfolioSelection", selection)
        writer.write_json("finalResponse", final_response)
        writer.write_json("sessionSnapshot", state)
        writer.write_json(
            "verifierReport",
            {
                "passed": all((item.get("verifier_json") or {}).get("passed") is True for item in proposals),
                "proposalReports": [
                    {
                        "proposalId": item.get("id"),
                        "verifier": item.get("verifier_json") or {},
                    }
                    for item in proposals
                ],
                "selectedProposalId": selected_id,
            },
        )
        writer.write_json("itinerarySnapshot", active_snapshot)
        writer.path("readme").write_text(
            "# Recorded dual-adoption artifact\n\nDeterministic recorded evidence; no live model call is claimed.\n",
            encoding="utf-8",
        )

        replay = RunArtifactReplayer().replay(writer.run_dir)
        if replay.get("status") != "success":
            raise ValueError(f"dual_artifact_replay_failed:{label}:{replay.get('errors')}")
        replay_records[label] = replay
        run_records.append(
            {
                "label": label,
                "artifactPath": writer.run_dir.relative_to(artifact_root).as_posix(),
                "fileSha256": _artifact_file_hashes(writer.run_dir),
            }
        )
        previous_state = state

    safe_export = redact_artifact(export_state)
    active_snapshot = safe_export.get("active_itinerary_snapshot")
    safe_export["selectedProposalId"] = selected_proposal_id
    safe_export["activeSnapshotFingerprint"] = (
        canonical_fingerprint(active_snapshot) if isinstance(active_snapshot, dict) and active_snapshot else None
    )
    _write_safe_json(artifact_root / "export-state.json", safe_export)
    _write_safe_json(artifact_root / "replay.json", replay_records)
    bundle_manifest = {
        "schemaVersion": "creative-portfolio-dual-adoption-artifact-v1",
        "sessionId": session_id,
        "portfolioId": portfolio_id,
        "selectedProposalId": selected_proposal_id,
        **fingerprints,
        "uncommittedEvidence": True,
        "providerMode": "recorded",
        "modelProvenance": "NOT_RUN(recorded deterministic fixture)",
        "runs": run_records,
        "exportState": "export-state.json",
        "replay": "replay.json",
    }
    _write_safe_json(artifact_root / "manifest.json", bundle_manifest)
    evaluation = evaluate_dual_adoption_artifact_bundle(artifact_root)
    if not evaluation["passed"]:
        raise ValueError(f"dual_artifact_integrity_failed:{evaluation}")
    return {
        "artifactRoot": artifact_root,
        "manifest": bundle_manifest,
        "evaluation": evaluation,
    }


def evaluate_dual_adoption_artifact_bundle(artifact_root: Path) -> dict[str, Any]:
    """Validate a portable bundle without trusting its precomputed summaries."""

    failures: list[str] = []
    manifest = _read_json(artifact_root / "manifest.json", failures)
    expected_labels = list(RUN_LABELS)
    run_records = manifest.get("runs") if isinstance(manifest.get("runs"), list) else []
    if [str(item.get("label") or "") for item in run_records] != expected_labels:
        failures.append("bundle_run_order_invalid")
    if manifest.get("schemaVersion") != "creative-portfolio-dual-adoption-artifact-v1":
        failures.append("bundle_schema_invalid")
    git_commit = str(manifest.get("gitCommit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", git_commit):
        failures.append("bundle_git_commit_invalid")
    for key in ("trackedDiffSha256", "statusSha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get(key) or "")):
            failures.append(f"bundle_{key}_invalid")
    if manifest.get("providerMode") != "recorded":
        failures.append("bundle_provider_mode_not_recorded")

    bundle_sequences: list[int] = []
    event_timestamps: list[datetime] = []
    selected_snapshot_fingerprints: dict[str, str | None] = {}
    replay_statuses: dict[str, str] = {}
    proposal_integrity_failures = 0
    canonical_mismatches: list[dict[str, str]] = []
    score_evidence_failures = 0
    lineage_failures = 0
    selected_ids: dict[str, str | None] = {}
    deltas: dict[str, list[int]] = {}
    active_versions: dict[str, str | None] = {}

    for record in run_records:
        label = str(record.get("label") or "")
        relative = Path(str(record.get("artifactPath") or ""))
        run_dir = (artifact_root / relative).resolve()
        try:
            run_dir.relative_to(artifact_root.resolve())
        except ValueError:
            failures.append(f"{label}:artifact_path_escape")
            continue
        declared_hashes = record.get("fileSha256") or {}
        if declared_hashes != _artifact_file_hashes(run_dir):
            failures.append(f"{label}:file_hash_mismatch")
        replay = RunArtifactReplayer().replay(run_dir)
        replay_statuses[label] = str(replay.get("status") or "")
        if replay.get("status") != "success":
            failures.append(f"{label}:replay_failed")
        run_manifest = _read_json(run_dir / "manifest.json", failures)
        if run_manifest.get("gitCommit") != git_commit:
            failures.append(f"{label}:git_commit_mismatch")
        if (run_manifest.get("providerStatus") or {}).get("mode") != "recorded":
            failures.append(f"{label}:provider_mode_not_recorded")
        final_response = _read_json(run_dir / "final_response.json", failures)
        selection = _read_json(run_dir / "portfolio_selection.json", failures)
        itinerary_snapshot = _read_json(
            run_dir / "itinerary_snapshot.json",
            failures,
            allow_null=True,
        )
        snapshot_fingerprint = (
            canonical_fingerprint(itinerary_snapshot)
            if isinstance(itinerary_snapshot, dict) and itinerary_snapshot
            else None
        )
        selected_snapshot_fingerprints[label] = snapshot_fingerprint
        if final_response.get("selectedSnapshotFingerprint") != snapshot_fingerprint:
            failures.append(f"{label}:final_response_snapshot_fingerprint_mismatch")
        if selection.get("selectedSnapshotFingerprint") != snapshot_fingerprint:
            failures.append(f"{label}:selection_snapshot_fingerprint_mismatch")
        selected_id = str(selection.get("selectedProposalId") or "") or None
        selected_ids[label] = selected_id
        if final_response.get("selectedProposalId") != selected_id:
            failures.append(f"{label}:assistant_selected_proposal_mismatch")
        active_versions[label] = str(final_response.get("activeVersionId") or "") or None
        deltas[label] = [int(selection.get(key) or 0) for key in ("versionDelta", "patchDelta", "routeWriteDelta")]
        if not str(final_response.get("assistantReply") or "").strip():
            failures.append(f"{label}:assistant_reply_missing")

        events = _read_jsonl(run_dir / "tool_events.jsonl", failures)
        if not events:
            failures.append(f"{label}:events_missing")
        for event in events:
            sequence = event.get("bundleSequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                failures.append(f"{label}:event_sequence_invalid")
            else:
                bundle_sequences.append(sequence)
            timestamp = _aware_datetime(event.get("timestamp"))
            if timestamp is None:
                failures.append(f"{label}:event_timestamp_invalid")
            else:
                event_timestamps.append(timestamp)
            duration = event.get("durationMs")
            if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
                failures.append(f"{label}:event_duration_invalid")
            if not str(event.get("providerName") or "").strip():
                failures.append(f"{label}:event_provider_missing")
            provider_metadata = event.get("providerMetadata")
            if not isinstance(provider_metadata, dict) or provider_metadata.get("mode") != "recorded":
                failures.append(f"{label}:event_provider_metadata_missing")

        proposals = _read_jsonl(run_dir / "plan_proposals.jsonl", failures)
        scores = _read_jsonl(run_dir / "portfolio_scores.jsonl", failures)
        verifiers = _read_jsonl(run_dir / "portfolio_verifier.jsonl", failures)
        if len(proposals) != 2 or len(scores) != 2 or len(verifiers) != 2:
            failures.append(f"{label}:portfolio_evidence_count_invalid")
        for proposal in proposals:
            snapshot = proposal.get("snapshot_json")
            if not isinstance(snapshot, dict):
                proposal_integrity_failures += 1
                continue
            recomputed = proposal_canonical_signature(snapshot)
            if recomputed != proposal.get("canonical_signature"):
                proposal_integrity_failures += 1
                canonical_mismatches.append(
                    {
                        "proposalId": str(proposal.get("id") or ""),
                        "stored": str(proposal.get("canonical_signature") or ""),
                        "recomputed": recomputed,
                    }
                )
            evidence = (proposal.get("score_json") or {}).get("evidence")
            if not isinstance(evidence, dict) or not SCORE_EVIDENCE_FIELDS <= set(evidence):
                score_evidence_failures += 1
            lineage = proposal.get("generation_lineage_json")
            if not _artifact_lineage_complete(lineage):
                lineage_failures += 1

    if bundle_sequences != list(range(1, len(bundle_sequences) + 1)):
        failures.append("bundle_event_sequence_not_contiguous")
    if event_timestamps != sorted(event_timestamps):
        failures.append("bundle_event_timestamps_not_monotonic")
    if selected_ids.get("turn_1") is not None:
        failures.append("turn_1_selected_proposal_present")
    selected = str(manifest.get("selectedProposalId") or "") or None
    if selected_ids.get("turn_2") != selected or selected_ids.get("turn_3_duplicate") != selected:
        failures.append("selected_proposal_not_stable")
    if deltas != {
        "turn_1": [0, 0, 0],
        "turn_2": [1, 1, 6],
        "turn_3_duplicate": [0, 0, 0],
    }:
        failures.append("bundle_version_patch_route_deltas_invalid")
    if not active_versions.get("turn_2") or active_versions.get("turn_2") != active_versions.get("turn_3_duplicate"):
        failures.append("bundle_active_version_not_stable")

    export_state = _read_json(artifact_root / "export-state.json", failures)
    export_snapshot = export_state.get("active_itinerary_snapshot")
    export_fingerprint = (
        canonical_fingerprint(export_snapshot) if isinstance(export_snapshot, dict) and export_snapshot else None
    )
    if export_state.get("readOnly") is not True:
        failures.append("export_state_not_read_only")
    if export_state.get("selectedProposalId") != selected:
        failures.append("export_state_selected_proposal_mismatch")
    if export_state.get("activeSnapshotFingerprint") != export_fingerprint:
        failures.append("export_state_fingerprint_invalid")
    if export_fingerprint != selected_snapshot_fingerprints.get("turn_3_duplicate"):
        failures.append("export_state_snapshot_mismatch")
    persisted_replay = _read_json(artifact_root / "replay.json", failures)
    if any((persisted_replay.get(label) or {}).get("status") != "success" for label in RUN_LABELS):
        failures.append("persisted_replay_failed")

    privacy_failures = _privacy_failures(artifact_root)
    failures.extend(privacy_failures)
    if proposal_integrity_failures:
        failures.append("artifact_canonical_signature_mismatch")
    if score_evidence_failures:
        failures.append("artifact_score_evidence_incomplete")
    if lineage_failures:
        failures.append("artifact_lineage_incomplete")
    return {
        "schemaVersion": "creative-portfolio-dual-artifact-eval-v1",
        "passed": not failures,
        "failures": sorted(set(failures)),
        "runCount": len(run_records),
        "replaySuccessCount": sum(1 for label in RUN_LABELS if replay_statuses.get(label) == "success"),
        "canonicalSignatureMismatchCount": proposal_integrity_failures,
        "canonicalSignatureMismatches": canonical_mismatches,
        "scoreEvidenceMissingCount": score_evidence_failures,
        "lineageIntegrityMismatchCount": lineage_failures,
        "privacyFailureCount": len(privacy_failures),
        "eventCount": len(bundle_sequences),
    }


def _recorded_turn_events(
    *,
    label: str,
    state: dict[str, Any],
    response: dict[str, Any],
    portfolio_id: str,
) -> list[dict[str, Any]]:
    raw = [
        item for key in ("planningSteps", "toolEvents") for item in response.get(key) or [] if isinstance(item, dict)
    ]
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        fingerprint = canonical_fingerprint(item)
        if fingerprint not in seen:
            seen.add(fingerprint)
            deduped.append(item)
    if deduped:
        return deduped
    proposals = _proposals_from_state(state, portfolio_id)
    events = [
        {
            "type": "portfolio_proposal_persisted",
            "label": "方案已物化并通过验证",
            "status": "completed",
            "detail": "recorded proposal row persisted",
            "timestamp": proposal.get("created_at"),
            "durationMs": 0,
            "providerName": "creative-portfolio-recorded-fixture",
            "metadata": {
                "proposalId": proposal.get("id"),
                "verifierPassed": (proposal.get("verifier_json") or {}).get("passed"),
            },
        }
        for proposal in proposals
    ]
    portfolio = _portfolio_from_state(state, portfolio_id)
    events.append(
        {
            "type": "portfolio_selection_checkpoint_persisted",
            "label": "方案比较等待选择",
            "status": "completed",
            "detail": "recorded portfolio checkpoint persisted",
            "timestamp": portfolio.get("updated_at") or portfolio.get("created_at"),
            "durationMs": 0,
            "providerName": "trip-server",
            "metadata": {
                "portfolioId": portfolio_id,
                "visibleProposalIds": (portfolio.get("summary_json") or {}).get("visibleProposalIds"),
            },
        }
    )
    return events


def _normalize_events(
    events: list[dict[str, Any]],
    *,
    label: str,
    starting_bundle_sequence: int,
    state: dict[str, Any],
    response: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    fallback_timestamp = _response_timestamp(response, state)
    result: list[dict[str, Any]] = []
    bundle_sequence = starting_bundle_sequence
    for local_sequence, source in enumerate(events, start=1):
        bundle_sequence += 1
        timestamp = str(source.get("timestamp") or fallback_timestamp)
        if _aware_datetime(timestamp) is None:
            raise ValueError(f"dual_artifact_event_timestamp_invalid:{label}")
        duration = source.get("durationMs")
        duration = duration if isinstance(duration, int) and duration >= 0 else 0
        result.append(
            {
                **source,
                "sequence": local_sequence,
                "sourceSequence": source.get("sequence"),
                "bundleSequence": bundle_sequence,
                "timestamp": timestamp,
                "durationMs": duration,
                "providerName": str(source.get("providerName") or "trip-server"),
                "providerMetadata": {
                    "mode": "recorded",
                    "source": ("persisted-event" if source.get("providerName") else "server-event-default"),
                },
                "recordedTurn": label,
            }
        )
    return result, bundle_sequence


def _response_timestamp(response: dict[str, Any], state: dict[str, Any]) -> str:
    assistant = response.get("assistantTurn") or {}
    for key in ("updatedAt", "createdAt"):
        if str(assistant.get(key) or ""):
            return str(assistant[key])
    turns = state.get("conversation_turns") or []
    for turn in reversed(turns):
        if str(turn.get("role") or "") == "assistant":
            return str(turn.get("updated_at") or turn.get("created_at") or "")
    return datetime.now(timezone.utc).isoformat()


def _assistant_reply(response: dict[str, Any]) -> str:
    assistant = response.get("assistantTurn") or {}
    return str(response.get("assistantReply") or response.get("reply") or assistant.get("content") or "")


def _portfolio_from_state(state: dict[str, Any], portfolio_id: str) -> dict[str, Any]:
    for item in state.get("plan_portfolios") or []:
        if str(item.get("id") or "") == portfolio_id:
            return dict(item)
    raise ValueError("dual_artifact_portfolio_missing")


def _proposals_from_state(state: dict[str, Any], portfolio_id: str) -> list[dict[str, Any]]:
    return sorted(
        [
            dict(item)
            for item in state.get("plan_proposals") or []
            if str(item.get("portfolio_id") or "") == portfolio_id
        ],
        key=lambda item: (int(item.get("rank_index") or 0), str(item.get("created_at") or "")),
    )


def _state_deltas(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> list[int]:
    if previous is None:
        previous = {}
    return [
        len(current.get("itinerary_versions") or []) - len(previous.get("itinerary_versions") or []),
        len(current.get("itinerary_patches") or []) - len(previous.get("itinerary_patches") or []),
        _selected_route_count(current.get("active_itinerary_snapshot"))
        - _selected_route_count(previous.get("active_itinerary_snapshot")),
    ]


def _selected_route_count(snapshot: Any) -> int:
    if not isinstance(snapshot, dict):
        return 0
    return sum(
        1 for item in snapshot.get("routeOptions") or [] if isinstance(item, dict) and item.get("isSelected") is True
    )


def _new_rows(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    *,
    key: str,
    identity_key: str,
) -> list[dict[str, Any]]:
    previous_ids = {
        str(item.get(identity_key) or "") for item in (previous or {}).get(key) or [] if isinstance(item, dict)
    }
    return [
        dict(item)
        for item in current.get(key) or []
        if isinstance(item, dict) and str(item.get(identity_key) or "") not in previous_ids
    ]


def _artifact_file_hashes(run_dir: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_dir.iterdir(), key=lambda item: item.name)
        if path.is_file()
    }


def _artifact_lineage_complete(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    projection = value.get("briefPlanningProjection")
    pareto = value.get("paretoSelection")
    return bool(
        value.get("optimizer") == "bounded_portfolio_optimizer"
        and isinstance(value.get("providerParser"), dict)
        and isinstance(value.get("portfolioRequiredCandidateBindings"), list)
        and value.get("portfolioRequiredCandidateBindings")
        and isinstance(projection, dict)
        and projection.get("briefId")
        and projection.get("dayRoleSignature")
        and isinstance(projection.get("dayEvidence"), dict)
        and isinstance(pareto, dict)
        and pareto.get("selector") == "pareto_portfolio_selector"
        and pareto.get("eligible") is True
    )


def _aware_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _read_json(
    path: Path,
    failures: list[str],
    *,
    allow_null: bool = False,
) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        failures.append(f"{path.name}:invalid_json")
        return None if allow_null else {}
    if value is None and allow_null:
        return None
    if not isinstance(value, dict):
        failures.append(f"{path.name}:json_object_required")
        return {}
    return value


def _read_jsonl(path: Path, failures: list[str]) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        failures.append(f"{path.name}:missing")
        return []
    result: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            failures.append(f"{path.name}:invalid_jsonl")
            continue
        if not isinstance(value, dict):
            failures.append(f"{path.name}:jsonl_object_required")
            continue
        result.append(value)
    return result


def _write_safe_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(redact_artifact(payload), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _privacy_failures(root: Path) -> list[str]:
    failures: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".json", ".jsonl", ".md"}:
            continue
        text = path.read_text(encoding="utf-8")
        if LOCAL_ABSOLUTE_PATH_RE.search(text):
            failures.append(f"{path.name}:local_absolute_path")
        if SECRET_VALUE_RE.search(text):
            failures.append(f"{path.name}:credential_value")
        if path.suffix == ".md":
            continue
        values: list[Any] = []
        for line in text.splitlines() if path.suffix == ".jsonl" else [text]:
            if line.strip():
                try:
                    values.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        for value in values:
            failures.extend(_privacy_value_failures(value, path.name))
    return sorted(set(failures))


def _privacy_value_failures(value: Any, filename: str) -> list[str]:
    failures: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in FORBIDDEN_REASONING_KEYS:
                failures.append(f"{filename}:hidden_reasoning_key")
            secret_marker = item is None or (
                isinstance(item, str) and item in {"[redacted]", "present", "empty", "missing"}
            )
            if is_secret_key(str(key)) and not secret_marker:
                failures.append(f"{filename}:secret_key_not_redacted")
            failures.extend(_privacy_value_failures(item, filename))
    elif isinstance(value, list):
        for item in value:
            failures.extend(_privacy_value_failures(item, filename))
    elif isinstance(value, str) and LOCAL_ABSOLUTE_PATH_RE.search(value):
        failures.append(f"{filename}:local_absolute_path")
    return failures
