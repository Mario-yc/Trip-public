import json
import hashlib
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.runtime.runtime_models import RuntimeRunOptions
from src.runtime.recorded_amap_replay import recorded_amap_replay_scope
from src.api.schemas.agent import AgentSessionCreateRequest
from src.api.schemas.itinerary_patches import ItineraryPatchOperation
from src.services.itinerary_patch_service import ItineraryPatchService
from src.services.poi_trust_policy import PoiTrustPolicy
from src.services.campus_candidate_policy import CampusCandidatePolicy
from src.services.conversation_service import ConversationService
from src.services.map_poi_service import AMAP_PLACE_SOURCE, MapPoiService
from src.services.route_service import RouteService
from src.services.agent_autonomy_service import AgentAutonomyController
from src.services.agent_observation_service import AgentObservationBuilder
from src.models.poi import POI


WEAK_NIGHT_VIEW_RE = re.compile(
    r"(酒店|宾馆|旅馆|民宿|公寓|住宿|图书馆|文化中心|文化馆|公司|写字楼|办公楼|服务中心|游客中心|管理处|管理中心|"
    r"售票|票务|门票|拍照|摄影|留念|购物小店|商店|专卖店|水站|停车场|医院|学校内部|教学楼|食堂|宿舍|管理局|办事处)"
)
LOW_LIKE_RISK = {"low", "ideal", "neutral"}
MEAL_PENDING_STATUSES = {"waiting_for_poi_grounding", "provider_rate_limited"}
ORDINARY_MEAL_STATUSES = {"", "not_required", "draft_only"}
LOCAL_FOOD_POSITIVE_RE = re.compile(r"(当地|本地|地方|特色|风味|老字号|小吃|菜系|夜市|美食街|传统)")
INSTITUTIONAL_MEAL_RE = re.compile(r"(食堂|教工餐厅|学生餐厅|员工餐厅|园区食堂|公司|单位|机关|宿舍)")
HOTEL_MEAL_RE = re.compile(r"(住宿服务|宾馆酒店|酒店|宾馆|旅馆|民宿|公寓|饭店住宿)")


class AgentQualityEvaluator:
    def __init__(self, runtime):
        self.runtime = runtime
        self.poi_trust_policy = PoiTrustPolicy()
        self.campus_candidate_policy = CampusCandidatePolicy()

    def run(
        self,
        *,
        scenario_file: str,
        state_dir: str,
        mock_providers: bool = False,
        mock_map_provider: Optional[dict[str, Any]] = None,
        baseline_ref: str = "preview/agent-mvp-foundation-20260629-agentcli",
        debug: bool = False,
        max_scenarios: Optional[int] = None,
        artifact_root: Optional[str] = None,
    ) -> dict[str, Any]:
        scenario_path = Path(scenario_file)
        payload = json.loads(scenario_path.read_text(encoding="utf-8"))
        scenarios = list(payload.get("scenarios") or [])
        if max_scenarios is not None:
            scenarios = scenarios[: max(0, max_scenarios)]

        results: list[dict[str, Any]] = []
        file_mock_map_provider = payload.get("defaultMockMapProvider")
        for scenario in scenarios:
            if artifact_root:
                result = self._score_existing_scenario(scenario, artifact_root=artifact_root)
            else:
                scenario_mock_map_provider = (
                    dict(file_mock_map_provider) if isinstance(file_mock_map_provider, dict) else mock_map_provider
                ) or ({"enabled": True} if self._should_default_mock_map_provider(scenario, mock_providers) else {})
                result = self._run_scenario(
                    scenario,
                    scenario_file=scenario_file,
                    state_dir=state_dir,
                    mock_providers=mock_providers,
                    mock_map_provider=scenario_mock_map_provider,
                    baseline_ref=baseline_ref,
                    debug=debug,
                )
            result.setdefault("executorOnly", bool(scenario.get("executorOnly")))
            result.setdefault("controlOwnershipExcluded", bool(scenario.get("controlOwnershipExcluded")))
            results.append(result)

        passed = sum(1 for item in results if item["status"] == "passed")
        failed = len(results) - passed
        aggregate_metrics, aggregate_failures = self._aggregate_metrics(
            results, payload.get("aggregateExpectations") or {}
        )
        return {
            "schemaVersion": "trip-agent-quality-eval-v1",
            "status": "passed" if failed == 0 and not aggregate_failures else "failed",
            "scenarioFile": os.fspath(scenario_path),
            "mode": "artifact_rescore" if artifact_root else "live_run",
            "summary": {"total": len(results), "passed": passed, "failed": failed},
            "aggregateMetrics": aggregate_metrics,
            "aggregateFailures": aggregate_failures,
            "results": results,
        }

    def _aggregate_metrics(
        self, results: list[dict[str, Any]], expectations: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        families = [
            str((item.get("metrics") or {}).get("firstNightFamily") or "")
            for item in results
            if str((item.get("metrics") or {}).get("firstNightFamily") or "")
        ]
        counts = {family: families.count(family) for family in sorted(set(families))}
        sample_count = len(families)
        max_share = (max(counts.values()) / sample_count) if sample_count and counts else 0.0
        signatures = [
            str((item.get("metrics") or {}).get("fullItinerarySignature") or "")
            for item in results
            if str((item.get("metrics") or {}).get("fullItinerarySignature") or "")
        ]
        signature_counts = {signature: signatures.count(signature) for signature in sorted(set(signatures))}
        signature_max_share = (
            max(signature_counts.values()) / len(signatures) if signatures and signature_counts else 0.0
        )
        metrics = {
            "firstNightSampleCount": sample_count,
            "distinctFirstNightFamilyCount": len(counts),
            "firstNightFamilyCounts": counts,
            "maxFirstNightFamilyShare": round(max_share, 4),
            "fullItinerarySampleCount": len(signatures),
            "distinctFullItinerarySignatureCount": len(signature_counts),
            "fullItinerarySignatureCounts": signature_counts,
            "maxFullItinerarySignatureShare": round(signature_max_share, 4),
        }
        control_eligible = [
            item for item in results if not item.get("executorOnly") and not item.get("controlOwnershipExcluded")
        ]
        metrics.update(
            {
                "controlOwnershipEligibleScenarioCount": len(control_eligible),
                "controlOwnershipExcludedScenarioCount": len(results) - len(control_eligible),
                "preControllerDomainRouterCount": sum(
                    int((item.get("metrics") or {}).get("preControllerDomainRouterCount") or 0)
                    for item in control_eligible
                ),
                "localMutationBypassCount": sum(
                    int((item.get("metrics") or {}).get("localMutationBypassCount") or 0) for item in control_eligible
                ),
            }
        )
        failures: list[str] = []
        minimum_samples = expectations.get("minFirstNightSampleCount")
        if minimum_samples is not None and sample_count < int(minimum_samples):
            failures.append(f"first-night sample count below {int(minimum_samples)}: {sample_count}")
        minimum_families = expectations.get("minDistinctFirstNightFamilies")
        if minimum_families is not None and len(counts) < int(minimum_families):
            failures.append(f"distinct first-night families below {int(minimum_families)}: {len(counts)}")
        maximum_share = expectations.get("maxFirstNightFamilyShare")
        if maximum_share is not None and max_share > float(maximum_share):
            failures.append(f"first-night family max share exceeded: {max_share:.4f} > {float(maximum_share):.4f}")
        minimum_signature_samples = expectations.get("minFullItinerarySampleCount")
        if minimum_signature_samples is not None and len(signatures) < int(minimum_signature_samples):
            failures.append(f"full-itinerary sample count below {int(minimum_signature_samples)}: {len(signatures)}")
        minimum_signatures = expectations.get("minDistinctFullItinerarySignatures")
        if minimum_signatures is not None and len(signature_counts) < int(minimum_signatures):
            failures.append(
                f"distinct full-itinerary signatures below {int(minimum_signatures)}: {len(signature_counts)}"
            )
        maximum_signature_share = expectations.get("maxFullItinerarySignatureShare")
        if maximum_signature_share is not None and signature_max_share > float(maximum_signature_share):
            failures.append(
                "full-itinerary signature max share exceeded: "
                f"{signature_max_share:.4f} > {float(maximum_signature_share):.4f}"
            )
        for key in ("preControllerDomainRouterCount", "localMutationBypassCount"):
            if key in expectations and int(metrics.get(key) or 0) != int(expectations[key]):
                failures.append(f"{key} mismatch: expected {int(expectations[key])}, got {int(metrics.get(key) or 0)}")
        return metrics, failures

    def _score_existing_scenario(self, scenario: dict[str, Any], *, artifact_root: str) -> dict[str, Any]:
        scenario_id = str(scenario.get("id") or "unnamed_scenario")
        artifact_path = self._latest_artifact_path(Path(artifact_root), scenario_id)
        artifact = self._read_artifact(artifact_path)
        final_response = artifact.get("finalResponse") or {}
        metrics = self._metrics(artifact, final_response)
        failures = self._expectation_failures(metrics, scenario.get("expectations") or {})
        return {
            "scenarioId": scenario_id,
            "status": "passed" if not failures else "failed",
            "artifactPath": os.fspath(artifact_path),
            "artifactPathAbsolute": os.fspath(artifact_path.resolve()),
            "runtimeStatus": final_response.get("status", "unknown"),
            "metrics": metrics,
            "failures": failures,
        }

    def _latest_artifact_path(self, artifact_root: Path, scenario_id: str) -> Path:
        scenario_dir = artifact_root / self._safe_id(scenario_id)
        run_dirs = sorted(path for path in scenario_dir.glob("run_*") if path.is_dir())
        if not run_dirs:
            return scenario_dir
        return run_dirs[-1]

    def _run_scenario(
        self,
        scenario: dict[str, Any],
        *,
        scenario_file: str,
        state_dir: str,
        mock_providers: bool,
        mock_map_provider: dict[str, Any],
        baseline_ref: str,
        debug: bool,
    ) -> dict[str, Any]:
        if isinstance(scenario.get("turns"), list) and scenario["turns"]:
            return self._run_turn_scenario(
                scenario,
                scenario_file=scenario_file,
                state_dir=state_dir,
                mock_providers=mock_providers,
                mock_map_provider=self._mock_map_provider_config(scenario, mock_map_provider),
                baseline_ref=baseline_ref,
                debug=debug,
            )
        if isinstance(scenario.get("recordedAmapProbe"), dict):
            return self._run_recorded_amap_probe(scenario)
        if isinstance(scenario.get("mockArtifact"), dict):
            return self._score_inline_artifact(scenario, state_dir=state_dir)
        scenario_id = str(scenario.get("id") or "unnamed_scenario")
        scenario_state_dir = Path(state_dir) / self._safe_id(scenario_id)
        options = RuntimeRunOptions(
            input=str(scenario.get("input") or ""),
            city=str(scenario.get("city") or "北京"),
            stateDir=os.fspath(scenario_state_dir),
            json=True,
            mockProviders=mock_providers,
            mockMapProvider=self._mock_map_provider_config(scenario, mock_map_provider),
            baselineRef=baseline_ref,
            debug=debug,
        )
        final, exit_code = self.runtime.run_once(
            options,
            argv=["trip-agent", "eval", "--scenario-file", scenario_file],
        )
        artifact_path = Path(final.artifact_path)
        artifact = self._read_artifact(artifact_path)
        metrics = self._metrics(artifact, final.model_dump(by_alias=True))
        failures = self._expectation_failures(metrics, scenario.get("expectations") or {})
        expected_terminal = str((scenario.get("expectations") or {}).get("terminalStatus") or "")
        acceptable_nonzero_terminal = bool(
            expected_terminal and expected_terminal == str(metrics.get("terminalStatus") or final.status)
        ) or bool(
            (scenario.get("expectations") or {}).get("mustCreateTimeline")
            and metrics.get("activeVersionCreated")
            and final.status == "needs_confirmation"
        )
        if exit_code != 0 and not failures and not acceptable_nonzero_terminal:
            failures.append(f"runtime exit code {exit_code} with status {final.status}")
        return {
            "scenarioId": scenario_id,
            "status": "passed" if not failures else "failed",
            "artifactPath": final.artifact_path,
            "artifactPathAbsolute": final.artifact_path_absolute,
            "runtimeStatus": final.status,
            "metrics": metrics,
            "failures": failures,
        }

    def _run_recorded_amap_probe(self, scenario: dict[str, Any]) -> dict[str, Any]:
        scenario_id = str(scenario.get("id") or "recorded_amap_probe")
        probe = scenario.get("recordedAmapProbe") or {}
        fixture_path = Path(str(probe.get("fixture") or ""))
        if not fixture_path.is_absolute():
            fixture_path = Path.cwd() / fixture_path
        if not fixture_path.exists():
            fixture_path = Path(__file__).resolve().parents[3] / str(probe.get("fixture") or "")
        failures: list[str] = []
        metrics: dict[str, Any] = {"recordedReplayPassed": False, "networkCallCount": 0}
        try:
            with recorded_amap_replay_scope(fixture_path) as replay:
                map_service = MapPoiService()
                city = str(probe.get("city") or "北京")
                category = str(probe.get("category") or "campus")
                from_result = map_service.search(city, str(probe.get("fromQuery") or ""), category, limit=5)
                to_result = map_service.search(city, str(probe.get("toQuery") or ""), category, limit=5)
                if not from_result.pois or not to_result.pois:
                    raise RuntimeError("recorded POI replay returned no ranked candidates")
                from_map_poi, to_map_poi = from_result.pois[0], to_result.pois[0]

                def domain_poi(item) -> POI:
                    return POI(
                        id=item.id,
                        amap_id=item.id,
                        name=item.name,
                        city=item.city,
                        category=item.category,
                        latitude=item.latitude,
                        longitude=item.longitude,
                        source=item.source,
                        confidence=0.99,
                        type=item.type,
                        district=item.district,
                        address=item.address,
                        source_note="sanitized recorded AMap response replay",
                    )

                route = RouteService()._build_amap_route(
                    "recorded_probe",
                    1,
                    1,
                    domain_poi(from_map_poi),
                    domain_poi(to_map_poi),
                    str(probe.get("mode") or "transit"),
                )
                controller_fixture_path = Path(str(probe.get("controllerFixture") or ""))
                if not controller_fixture_path.is_absolute():
                    controller_fixture_path = Path.cwd() / controller_fixture_path
                if not controller_fixture_path.exists():
                    controller_fixture_path = Path(__file__).resolve().parents[3] / str(
                        probe.get("controllerFixture") or ""
                    )
                controller_recording = json.loads(controller_fixture_path.read_text(encoding="utf-8"))
                if controller_recording.get("schemaVersion") != "trip-recorded-controller-v1":
                    raise RuntimeError("unsupported recorded Controller fixture schema")
                raw_decisions = controller_recording.get("decisions") or []
                if not raw_decisions:
                    raise RuntimeError("recorded Controller fixture has no decisions")

                class RecordedControllerProvider:
                    def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
                        del timeout_seconds, repair_feedback
                        return json.dumps(raw_decisions[0], ensure_ascii=False)

                recorded_candidate = from_map_poi.model_dump(by_alias=True)
                controller_context = {
                    "latestUserMessage": "使用唯一安全候选",
                    "effectiveUserMessage": "使用唯一安全候选",
                    "activeVersionId": "ver_recorded",
                    "currentItinerarySnapshot": {
                        "id": "plan_recorded",
                        "versionId": "ver_recorded",
                        "days": [
                            {
                                "id": "day_recorded",
                                "dayNumber": 1,
                                "segments": [{"id": "seg_recorded", "title": "待确认地点"}],
                            }
                        ],
                    },
                    "pendingAmapPoiCandidates": [
                        {
                            "id": "candidate_recorded",
                            "sourceSegmentId": "seg_recorded",
                            "status": "pending",
                            "candidates": [recorded_candidate],
                        }
                    ],
                    "runtimeLimits": {"remainingCycles": 2},
                }
                observation = AgentObservationBuilder().build(controller_context)
                controller_result = AgentAutonomyController(provider=RecordedControllerProvider()).decide(
                    "使用唯一安全候选",
                    controller_context,
                    {"agentObservation": observation.model_dump(by_alias=True)},
                    available_tools={"patch_itinerary"},
                    runtime_budget_tools={"patch_itinerary"},
                    observation=observation,
                )
                controller_metadata = controller_result.to_event_metadata()
                metrics.update(
                    {
                        "recordedReplayPassed": True,
                        "fromPoiName": from_map_poi.name,
                        "toPoiName": to_map_poi.name,
                        "routeDistanceMeters": route.distance_meters,
                        "routeDurationSeconds": route.duration_seconds,
                        "recordedRequestCount": len(replay.requests),
                        "recordedEndpoints": [request["endpoint"] for request in replay.requests],
                        "recordedControllerPassed": bool(
                            controller_metadata.get("source") == "controller"
                            and controller_metadata.get("accepted") is True
                            and controller_metadata.get("actionDirectiveSource") == "model"
                            and controller_metadata.get("targetScope", {}).get("candidateId")
                            in observation.target_inventory.candidate_ids
                            and controller_metadata.get("targetScope", {}).get("amapPoiId")
                            in observation.target_inventory.amap_poi_ids
                        ),
                        "recordedControllerPrimaryAction": controller_metadata.get("primaryAction"),
                    }
                )
        except Exception as error:
            failures.append(f"recorded AMap replay failed: {error}")
        expectations = scenario.get("expectations") or {}
        if expectations.get("recordedReplayMustPass") and not metrics.get("recordedReplayPassed"):
            failures.append("recorded AMap replay did not pass")
        if expectations.get("recordedControllerMustPass") and not metrics.get("recordedControllerPassed"):
            failures.append("recorded Controller replay did not pass")
        for key in (
            "fromPoiName",
            "toPoiName",
            "routeDistanceMeters",
            "routeDurationSeconds",
            "networkCallCount",
            "recordedRequestCount",
        ):
            if key in expectations and metrics.get(key) != expectations[key]:
                failures.append(f"{key} mismatch: expected {expectations[key]}, got {metrics.get(key)}")
        return {
            "scenarioId": scenario_id,
            "status": "passed" if not failures else "failed",
            "artifactPath": str(fixture_path),
            "artifactPathAbsolute": str(fixture_path.resolve()),
            "runtimeStatus": "success" if not failures else "validation_failed",
            "metrics": metrics,
            "failures": failures,
        }

    def _score_inline_artifact(self, scenario: dict[str, Any], *, state_dir: str) -> dict[str, Any]:
        scenario_id = str(scenario.get("id") or "unnamed_scenario")
        artifact_path = Path(state_dir) / self._safe_id(scenario_id) / "mock_artifact"
        artifact_path.mkdir(parents=True, exist_ok=True)
        artifact = self._normalize_inline_artifact(scenario.get("mockArtifact") or {})
        final_response = (
            scenario.get("mockFinalResponse") if isinstance(scenario.get("mockFinalResponse"), dict) else {}
        )
        if "activeVersionId" not in final_response:
            final_response = {**final_response, "activeVersionId": "ver_mock_risk_search"}
        self._write_inline_artifact(artifact_path, artifact, final_response)
        metrics = self._metrics(artifact, final_response)
        failures = self._expectation_failures(metrics, scenario.get("expectations") or {})
        return {
            "scenarioId": scenario_id,
            "status": "passed" if not failures else "failed",
            "artifactPath": os.fspath(artifact_path),
            "artifactPathAbsolute": os.fspath(artifact_path.resolve()),
            "runtimeStatus": "mock_artifact",
            "metrics": metrics,
            "failures": failures,
        }

    def _normalize_inline_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            "finalResponse": artifact.get("finalResponse") or {},
            "context": artifact.get("context") or {},
            "itinerarySnapshot": artifact.get("itinerarySnapshot") or {},
            "verifierReport": artifact.get("verifierReport") or {},
            "sessionSnapshot": artifact.get("sessionSnapshot") or {},
            "planningSteps": artifact.get("planningSteps") if isinstance(artifact.get("planningSteps"), list) else [],
            "toolEvents": artifact.get("toolEvents") if isinstance(artifact.get("toolEvents"), list) else [],
        }

    def _write_inline_artifact(
        self, artifact_path: Path, artifact: dict[str, Any], final_response: dict[str, Any]
    ) -> None:
        files = {
            "final_response.json": final_response,
            "context.json": artifact.get("context") or {},
            "itinerary_snapshot.json": artifact.get("itinerarySnapshot") or {},
            "verifier_report.json": artifact.get("verifierReport") or {},
            "session_snapshot.json": artifact.get("sessionSnapshot") or {},
        }
        for filename, payload in files.items():
            (artifact_path / filename).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        for filename, items in {
            "planning_steps.jsonl": artifact.get("planningSteps") or [],
            "tool_events.jsonl": artifact.get("toolEvents") or [],
        }.items():
            (artifact_path / filename).write_text(
                "".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in items),
                encoding="utf-8",
            )

    def _run_turn_scenario(
        self,
        scenario: dict[str, Any],
        *,
        scenario_file: str,
        state_dir: str,
        mock_providers: bool,
        mock_map_provider: dict[str, Any],
        baseline_ref: str,
        debug: bool,
    ) -> dict[str, Any]:
        scenario_id = str(scenario.get("id") or "unnamed_scenario")
        session_id: Optional[str] = None
        seed_itinerary = scenario.get("seedItinerary") if isinstance(scenario.get("seedItinerary"), dict) else None
        if seed_itinerary is not None:
            seeded_session = self.runtime.create_session(
                AgentSessionCreateRequest(
                    city=str(scenario.get("city") or "北京"),
                    title=f"CLI eval seed: {scenario_id}",
                )
            )
            seeded_snapshot = self._namespace_seed_ids(seed_itinerary, seeded_session.session_id)
            ItineraryPatchService(self.runtime.db).apply_patch(
                seeded_session.active_plan_id,
                [ItineraryPatchOperation(op="replace_itinerary", fullItinerary=seeded_snapshot)],
                source_type="manual",
                planning_context={"toolRefreshPolicy": {"route": "skip"}},
            )
            session_id = seeded_session.session_id
            for index, pending in enumerate(scenario.get("seedPendingPoiCandidates") or [], start=1):
                if not isinstance(pending, dict):
                    continue
                seeded_days = seeded_snapshot.get("days") if isinstance(seeded_snapshot.get("days"), list) else []
                seeded_segments = [
                    segment
                    for day in seeded_days
                    if isinstance(day, dict)
                    for segment in day.get("segments") or []
                    if isinstance(segment, dict)
                ]
                segment_id = (
                    str(pending.get("segmentId") or (seeded_segments[0].get("id") if seeded_segments else "")) or None
                )
                self.runtime.db.execute(
                    """
                    INSERT INTO amap_poi_candidates (
                        id, session_id, turn_id, query, segment_id, city, category, status,
                        candidates_json, selected_amap_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, ?)
                    """,
                    (
                        f"{str(pending.get('id') or f'candidate_eval_{index}')}_{seeded_session.session_id}",
                        seeded_session.session_id,
                        None,
                        str(pending.get("query") or "候选地点"),
                        segment_id,
                        str(scenario.get("city") or "北京"),
                        str(pending.get("category") or "scenic"),
                        json.dumps(pending.get("candidates") or [], ensure_ascii=False, default=str),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            self.runtime.db.commit()
        turn_results: list[dict[str, Any]] = []
        selected_choices: dict[int, dict[str, Any]] = {}
        final_status = "failed"
        final_artifact_path = ""
        final_artifact_path_absolute = ""
        for index, turn in enumerate(scenario.get("turns") or [], start=1):
            if not isinstance(turn, dict):
                continue
            scenario_state_dir = Path(state_dir) / self._safe_id(scenario_id) / f"turn_{index}"
            selected_choice = self._selected_choice_for_eval_turn(
                session_id,
                turn,
                selected_choices,
                turn_results,
            )
            if turn.get("forceActiveVersionIdBeforeTurn") and session_id:
                self.runtime.db.execute(
                    "UPDATE conversation_sessions SET active_version_id = ? WHERE id = ?",
                    (str(turn["forceActiveVersionIdBeforeTurn"]), session_id),
                )
                self.runtime.db.commit()
            options = RuntimeRunOptions(
                input=str(turn.get("input") or ""),
                city=str(turn.get("city") or scenario.get("city") or "北京"),
                sessionId=session_id,
                stateDir=os.fspath(scenario_state_dir),
                json=True,
                mockProviders=bool(turn.get("mockProviders", mock_providers)),
                mockMapProvider=self._mock_map_provider_config(turn, mock_map_provider),
                selectedAgentChoice=selected_choice,
                baselineRef=baseline_ref,
                debug=debug,
            )
            final, exit_code = self.runtime.run_once(
                options,
                argv=["trip-agent", "eval", "--scenario-file", scenario_file, "--turn", str(index)],
            )
            session_id = final.session_id or session_id
            if selected_choice:
                selected_choices[index] = selected_choice
            final_status = final.status
            final_artifact_path = final.artifact_path
            final_artifact_path_absolute = final.artifact_path_absolute
            artifact = self._read_artifact(Path(final.artifact_path))
            final_payload = final.model_dump(by_alias=True)
            turn_results.append(
                {
                    "turnIndex": index,
                    "input": str(turn.get("input") or ""),
                    "exitCode": exit_code,
                    "runtimeStatus": final.status,
                    "artifactPath": final.artifact_path,
                    "metrics": self._metrics(artifact, final_payload),
                    "context": artifact.get("context") or {},
                    "finalResponse": final_payload,
                    "planningSteps": artifact.get("planningSteps") or [],
                    "toolEvents": artifact.get("toolEvents") or [],
                    "sessionSnapshot": artifact.get("sessionSnapshot") or {},
                }
            )
        metrics = self._multi_turn_metrics(turn_results)
        failures = self._expectation_failures(metrics, scenario.get("expectations") or scenario.get("assertions") or {})
        expected_decisions = [str(item) for item in scenario.get("expectedDecisions") or [] if str(item)]
        actual_decisions = [
            action for item in turn_results for action, _route in self._turn_cycle_sequence(item) if action
        ]
        if expected_decisions and actual_decisions != expected_decisions:
            failures.append(f"decision sequence mismatch: expected {expected_decisions}, got {actual_decisions}")
        expected_actions = [str(item) for item in scenario.get("expectedActions") or [] if str(item)]
        actual_actions = [route for item in turn_results for _action, route in self._turn_cycle_sequence(item) if route]
        if expected_actions and actual_actions != expected_actions:
            failures.append(f"action sequence mismatch: expected {expected_actions}, got {actual_actions}")
        expected_version_delta = scenario.get("expectedVersionDelta")
        if expected_version_delta is not None and int(metrics.get("activeVersionChangeCount") or 0) != int(
            expected_version_delta
        ):
            failures.append(
                f"version delta mismatch: expected {int(expected_version_delta)}, got {metrics.get('activeVersionChangeCount')}"
            )
        expected_terminal_statuses = [str(item) for item in scenario.get("expectedTerminalStatuses") or [] if str(item)]
        expected_terminal_status = str(scenario.get("expectedTerminalStatus") or "")
        if expected_terminal_statuses and final_status not in expected_terminal_statuses:
            failures.append(
                f"terminal status mismatch: expected one of {expected_terminal_statuses}, got {final_status}"
            )
        elif expected_terminal_status and final_status != expected_terminal_status:
            failures.append(f"terminal status mismatch: expected {expected_terminal_status}, got {final_status}")
        turn_expectations = (
            scenario.get("turnExpectations") if isinstance(scenario.get("turnExpectations"), list) else []
        )
        for turn_index, expected in enumerate(turn_expectations, start=1):
            if not isinstance(expected, dict) or turn_index > len(turn_results):
                continue
            for failure in self._expectation_failures(turn_results[turn_index - 1]["metrics"], expected):
                failures.append(f"turn {turn_index}: {failure}")
        scenario_expectations = scenario.get("expectations") or scenario.get("assertions") or {}
        for turn in turn_results:
            expected = (
                turn_expectations[int(turn["turnIndex"]) - 1]
                if int(turn["turnIndex"]) <= len(turn_expectations)
                else {}
            )
            expected_non_success = isinstance(expected, dict) and str(expected.get("terminalStatus") or "") == str(
                turn.get("runtimeStatus") or ""
            )
            # Structured confirmation and root candidate refresh are safe
            # terminals for an autonomy scenario even when the CLI maps them
            # to exit 3.
            expected_non_success = expected_non_success or str(turn.get("runtimeStatus") or "") in {
                "needs_confirmation",
                "candidate_refresh_required",
            }
            combined_text = str((turn.get("metrics") or {}).get("combinedText") or "").lower()
            recovered_rate_limit = bool(
                int(turn.get("turnIndex") or 0) < len(turn_results)
                and scenario_expectations.get("activeVersionEventuallyCreated")
                and metrics.get("activeVersionCreated")
                and any(marker in combined_text for marker in ("rate limit", "provider_rate_limited", "限流"))
            )
            expected_non_success = expected_non_success or recovered_rate_limit
            if turn["exitCode"] != 0 and not expected_non_success:
                failures.append(
                    f"turn {turn['turnIndex']} runtime exit code {turn['exitCode']} with status {turn['runtimeStatus']}"
                )
        return {
            "scenarioId": scenario_id,
            "status": "passed" if not failures else "failed",
            "artifactPath": final_artifact_path,
            "artifactPathAbsolute": final_artifact_path_absolute,
            "runtimeStatus": final_status,
            "metrics": metrics,
            "turnResults": [
                {
                    "turnIndex": item["turnIndex"],
                    "runtimeStatus": item["runtimeStatus"],
                    "artifactPath": item["artifactPath"],
                    "actualAction": self._turn_action_type(item.get("metrics") or {}),
                    "metrics": item["metrics"],
                }
                for item in turn_results
            ],
            "failures": failures,
            "executorOnly": bool(scenario.get("executorOnly")),
            "controlOwnershipExcluded": bool(scenario.get("controlOwnershipExcluded")),
        }

    @staticmethod
    def _turn_cycle_sequence(turn: dict[str, Any]) -> list[tuple[str, str]]:
        context = turn.get("context") if isinstance(turn.get("context"), dict) else {}
        loop = context.get("agentControlLoop") if isinstance(context.get("agentControlLoop"), dict) else {}
        cycles = loop.get("cycles") if isinstance(loop.get("cycles"), list) else []
        sequence: list[tuple[str, str]] = []
        for cycle in cycles:
            if not isinstance(cycle, dict):
                continue
            decision = cycle.get("decision") if isinstance(cycle.get("decision"), dict) else {}
            outcome = cycle.get("outcome") if isinstance(cycle.get("outcome"), dict) else {}
            sequence.append(
                (
                    str(decision.get("primaryAction") or ""),
                    str(outcome.get("executionRoute") or decision.get("actualExecutionRoute") or ""),
                )
            )
        if sequence:
            return sequence
        planning_steps = turn.get("planningSteps") if isinstance(turn.get("planningSteps"), list) else []
        outcome_routes: dict[int, str] = {}
        for event in planning_steps:
            if not isinstance(event, dict) or event.get("type") != "agent_action_outcome":
                continue
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            preview = metadata.get("resultPreview") if isinstance(metadata.get("resultPreview"), dict) else {}
            cycle_index = metadata.get("cycleIndex")
            if isinstance(cycle_index, int):
                outcome_routes[cycle_index] = str(preview.get("executionRoute") or "")
        for event in planning_steps:
            if not isinstance(event, dict) or event.get("type") != "agent_decision":
                continue
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            cycle_index = metadata.get("cycleIndex")
            route = outcome_routes.get(cycle_index, "") if isinstance(cycle_index, int) else ""
            sequence.append(
                (
                    str(metadata.get("primaryAction") or ""),
                    route or str(metadata.get("actualExecutionRoute") or ""),
                )
            )
        if sequence:
            return sequence
        metrics = turn.get("metrics") if isinstance(turn.get("metrics"), dict) else {}
        return [
            (
                str(metrics.get("agentDecisionPrimaryAction") or ""),
                AgentQualityEvaluator._turn_action_type(metrics),
            )
        ]

    @staticmethod
    def _turn_action_type(metrics: dict[str, Any]) -> str:
        if int(metrics.get("candidateSelectionPatchCount") or 0) > 0:
            return "candidate_patch"
        if int(metrics.get("localPoiOptionCount") or 0) > 0:
            return "candidate_options"
        route = str(metrics.get("actualExecutionRoute") or "")
        if route:
            return route
        proposed = str(metrics.get("agentDecisionProposedExecutionRoute") or "")
        if metrics.get("agentDecisionPrimaryAction") == "read_itinerary" and proposed == "read_only":
            return "read_only"
        return str(metrics.get("executionMode") or "unknown")

    def _read_artifact(self, artifact_path: Path) -> dict[str, Any]:
        return {
            "finalResponse": self._read_json(artifact_path / "final_response.json"),
            "context": self._read_json(artifact_path / "context.json"),
            "agentDecision": self._read_json(artifact_path / "agent_decision.json"),
            "agentObservations": self._read_jsonl(artifact_path / "agent_observations.jsonl"),
            "agentDecisions": self._read_jsonl(artifact_path / "agent_decisions.jsonl"),
            "agentActionOutcomes": self._read_jsonl(artifact_path / "agent_action_outcomes.jsonl"),
            "agentStop": self._read_json(artifact_path / "agent_stop.json"),
            "itinerarySnapshot": self._read_json(artifact_path / "itinerary_snapshot.json"),
            "verifierReport": self._read_json(artifact_path / "verifier_report.json"),
            "sessionSnapshot": self._read_json(artifact_path / "session_snapshot.json"),
            "planningSteps": self._read_jsonl(artifact_path / "planning_steps.jsonl"),
            "toolEvents": self._read_jsonl(artifact_path / "tool_events.jsonl"),
            "planProposals": self._read_jsonl(artifact_path / "plan_proposals.jsonl"),
            "portfolio": self._read_json(artifact_path / "portfolio.json"),
            "portfolioSelection": self._read_json(artifact_path / "portfolio_selection.json"),
        }

    def _metrics(self, artifact: dict[str, Any], final_response: dict[str, Any]) -> dict[str, Any]:
        snapshot = artifact.get("itinerarySnapshot") or {}
        days = snapshot.get("days") if isinstance(snapshot.get("days"), list) else []
        pending_candidates = self._pending_candidates(artifact, final_response)
        ordinary_meals = self._ordinary_meal_placeholders(days)
        required_meals = self._required_meals(days)
        explicit_food = self._explicit_local_food_metrics(required_meals)
        untrusted_meal = self._untrusted_meal_metrics(days)
        route_quality = self._route_quality_metrics(artifact)
        preferred_mode = str(
            ((artifact.get("context") or {}).get("understoodRequirements") or {}).get("transportMode") or ""
        )
        if not preferred_mode:
            preferred_mode = self._preferred_transport_from_text(
                str((artifact.get("context") or {}).get("effectiveUserMessage") or "")
            )
        route_mode_metrics = self._preferred_route_mode_metrics(snapshot, preferred_mode)
        web_search_metrics = self._web_search_diagnostics_metrics(snapshot)
        timeline_edit_metrics = self._timeline_edit_metrics(artifact)
        route_pair_metrics = self._route_pair_metrics(
            snapshot,
            set(timeline_edit_metrics.get("timelineEditChangedSegmentIds") or []),
        )
        route_optimization_metrics = self._route_optimization_metrics(artifact)
        candidate_selection_metrics = self._candidate_selection_metrics(artifact)
        meal_cost_metrics = self._meal_cost_metrics(snapshot)
        tool_budget_metrics = self._tool_budget_metrics(artifact)
        latency_budget_metrics = self._latency_duration_budget_metrics(artifact, snapshot)
        creative_metrics = self._creative_variant_metrics(artifact, final_response)
        daily_goal_metrics = self._daily_goal_convergence_metrics(artifact, snapshot)
        pool_priority_metrics = self._intent_pool_priority_metrics(artifact)
        food_cardinality_metrics = self._food_experience_cardinality_metrics(artifact)
        runtime_debug = final_response.get("debug") if isinstance(final_response.get("debug"), dict) else {}
        session_snapshot = artifact.get("sessionSnapshot") if isinstance(artifact.get("sessionSnapshot"), dict) else {}
        turns = (
            session_snapshot.get("conversation_turns")
            if isinstance(session_snapshot.get("conversation_turns"), list)
            else []
        )
        persisted_response: dict[str, Any] = {}
        for turn in reversed(turns):
            candidate = turn.get("agent_response_json") if isinstance(turn, dict) else None
            if isinstance(candidate, dict):
                persisted_response = candidate
                break
        semantic_integrity_metrics = self._semantic_integrity_metrics(
            artifact,
            persisted_response=persisted_response,
        )
        fallback_confirmation_metrics = self._fallback_confirmation_metrics(session_snapshot)
        # Eval runs do not require CLI --debug, while execution semantics are
        # still part of the response contract. Preserve those top-level fields
        # so the quality gate does not turn missing debug output into None.
        runtime_debug = {
            **runtime_debug,
            "executionMode": runtime_debug.get("executionMode")
            or final_response.get("executionMode")
            or persisted_response.get("executionMode"),
            "terminalStatus": runtime_debug.get("terminalStatus")
            or final_response.get("terminalStatus")
            or final_response.get("status")
            or persisted_response.get("terminalStatus"),
        }
        timeline_mutation_outcome = (
            persisted_response.get("timelineMutationOutcome")
            if isinstance(persisted_response.get("timelineMutationOutcome"), dict)
            else {}
        )
        timeline_mutation_event = next(
            (
                event
                for event in artifact.get("planningSteps") or []
                if isinstance(event, dict) and event.get("type") == "timeline_mutation_detected"
            ),
            {},
        )
        timeline_mutation_event_metadata = (
            timeline_mutation_event.get("metadata") if isinstance(timeline_mutation_event.get("metadata"), dict) else {}
        )
        timeline_mutation_metrics = self._timeline_mutation_transaction_metrics(
            artifact,
            timeline_mutation_outcome,
        )

        verifier_report = artifact.get("verifierReport") if isinstance(artifact.get("verifierReport"), dict) else {}
        decision_bundle = artifact.get("agentDecision") if isinstance(artifact.get("agentDecision"), dict) else {}
        decision = (
            decision_bundle.get("rawDecision")
            if isinstance(decision_bundle.get("rawDecision"), dict)
            else decision_bundle
        )
        gated_decision = (
            decision_bundle.get("gatedDecision") if isinstance(decision_bundle.get("gatedDecision"), dict) else {}
        )
        decision_event = next(
            (
                item
                for item in artifact.get("planningSteps") or []
                if isinstance(item, dict) and item.get("type") == "agent_decision"
            ),
            {},
        )
        decision_metadata = (
            decision_event.get("metadata") if isinstance(decision_event.get("metadata"), dict) else gated_decision
        )
        all_decision_events = [
            item
            for item in (artifact.get("agentDecisions") or artifact.get("planningSteps") or [])
            if isinstance(item, dict) and item.get("type") == "agent_decision"
        ]
        decision_metadatas = [
            item.get("metadata") for item in all_decision_events if isinstance(item.get("metadata"), dict)
        ]
        outcome_events = (
            artifact.get("agentActionOutcomes") if isinstance(artifact.get("agentActionOutcomes"), list) else []
        )
        outcome_metadata = (
            outcome_events[-1].get("metadata", {}).get("resultPreview", {})
            if outcome_events and isinstance(outcome_events[-1], dict)
            else {}
        )
        initial_actual_execution_route = self._actual_execution_route_for_decision(
            decision_metadata,
            outcome_events,
        )
        continuing_actions = {
            "draft_itinerary",
            "resolve_poi",
            "patch_itinerary",
            "optimize_route",
            "verify_external_facts",
        }
        accepted_continuing_decisions = {
            (self._safe_int(metadata.get("cycleIndex")), str(metadata.get("primaryAction") or ""))
            for metadata in decision_metadatas
            if metadata.get("accepted") is True and str(metadata.get("primaryAction") or "") in continuing_actions
        }
        expected_executor_routes = {
            "draft_itinerary": {"staged_initial_pipeline"},
            "resolve_poi": {"poi_grounding"},
            "patch_itinerary": {
                "candidate_patch_executor",
                "patch_itinerary_executor",
                "deterministic_timeline_patch",
                "deterministic_timeline_then_bounded_tool_loop",
                "timeline_mutation_executor",
            },
            "optimize_route": {"route_optimization"},
            "verify_external_facts": {"derived_facts_only"},
        }
        continuing_action_outcomes = {
            (
                self._safe_int(self._event_result_preview(event).get("cycleIndex")),
                str(self._event_result_preview(event).get("action") or ""),
            )
            for event in outcome_events
            if isinstance(event, dict)
            and str(self._event_result_preview(event).get("action") or "") in continuing_actions
            and str(self._event_result_preview(event).get("executionRoute") or "")
            in expected_executor_routes.get(str(self._event_result_preview(event).get("action") or ""), set())
            and str(self._event_result_preview(event).get("status") or "") not in {"failed", "no_change"}
        }
        local_options = (
            persisted_response.get("localPoiOptions")
            if isinstance(persisted_response.get("localPoiOptions"), dict)
            else {}
        )
        routeable_by_day = {
            str(day.get("dayNumber") or index): self._routeable_anchor_count(day)
            for index, day in enumerate(days, start=1)
        }
        verified_write_event = any(
            isinstance(event, dict)
            and event.get("type") == "verify"
            and isinstance(event.get("metadata"), dict)
            and event["metadata"].get("passed") is True
            for event in artifact.get("planningSteps") or []
        )
        proposal_trace_keys = (
            "proposalPreviewWriteCount",
            "proposalCommitAttemptCount",
            "versionWriteCount",
            "patchWriteCount",
            "pendingSlotPreservedCount",
        )
        proposal_trace_metrics = {key: 0 for key in proposal_trace_keys}
        for event in artifact.get("planningSteps") or []:
            if not isinstance(event, dict) or not isinstance(event.get("metadata"), dict):
                continue
            metadata = event["metadata"]
            for key in proposal_trace_keys:
                if key in metadata:
                    proposal_trace_metrics[key] = self._safe_int(metadata.get(key))
        react_causal_metrics = self._react_causal_integrity_metrics(
            artifact,
            final_response,
            decision_metadatas,
            outcome_events,
        )
        return {
            **proposal_trace_metrics,
            "activeVersionCreated": bool(final_response.get("activeVersionId")) and bool(days),
            "activeVersionChanged": bool(final_response.get("activeVersionChanged")),
            "activeVersionId": final_response.get("activeVersionId"),
            "agentDecisionEventPresent": bool(decision_event),
            "agentDecisionPrimaryAction": decision.get("primaryAction") or decision_metadata.get("primaryAction"),
            "agentDecisionRequiredTools": list(
                decision.get("requiredTools") or decision_metadata.get("requiredTools") or []
            ),
            "agentDecisionEffectiveTools": list(decision_metadata.get("effectiveTools") or []),
            "agentDecisionEffectiveWriteRisk": decision_metadata.get("effectiveWriteRisk"),
            "agentDecisionPolicyAccepted": decision_metadata.get("accepted"),
            "agentDecisionStopConditionType": (
                decision.get("stopCondition") or decision_metadata.get("stopCondition") or {}
            ).get("type"),
            "agentDecisionSource": decision_metadata.get("source"),
            "agentDecisionProposedExecutionRoute": decision_metadata.get("proposedExecutionRoute"),
            "agentDecisionDurationMs": self._safe_int(decision_metadata.get("decisionDurationMs")),
            "controllerError": decision_metadata.get("controllerError"),
            "actualExecutionRoute": initial_actual_execution_route
            or (artifact.get("context") or {}).get("actualExecutionRoute")
            or persisted_response.get("actualExecutionRoute")
            or timeline_mutation_event_metadata.get("actualExecutionRoute"),
            "outcomeVerifierPassed": outcome_metadata.get("verifierPassed"),
            "rollbackPerformed": bool(outcome_metadata.get("rollbackPerformed")),
            "autonomyObservationCount": len(artifact.get("agentObservations") or []),
            "autonomyActionOutcomeCount": len(artifact.get("agentActionOutcomes") or []),
            "unexecutedAcceptedContinuingDecisionCount": len(
                accepted_continuing_decisions - continuing_action_outcomes
            ),
            "autonomyStopReason": (artifact.get("agentStop") or {})
            .get("metadata", {})
            .get("resultPreview", {})
            .get("stopReason")
            if isinstance(artifact.get("agentStop"), dict)
            else None,
            "decisionEventBeforeAction": self._decision_event_before_action(artifact.get("planningSteps") or []),
            "naturalLanguageTurnCount": 0 if (artifact.get("context") or {}).get("selectedAgentChoice") else 1,
            "modelPrimaryDecisionCount": int(decision_metadata.get("source") == "controller"),
            "modelPrimaryDecisionRatio": float(decision_metadata.get("source") == "controller"),
            "deterministicBusinessPreemptionCount": sum(
                1
                for metadata in decision_metadatas[:1]
                if metadata.get("source") in {"deterministic_arbitrator", "planner_fallback"}
                and not metadata.get("controllerError")
            ),
            "plannerCalledOnHealthyControllerCount": sum(
                1
                for metadata in decision_metadatas
                if metadata.get("source") == "controller" and metadata.get("plannerCalled") is True
            ),
            "timelineParserPreDecisionCount": sum(
                1 for metadata in decision_metadatas if metadata.get("timelineParserCalledBeforeDecision") is True
            ),
            "legacyPhaseRouterPreemptionCount": sum(
                1 for metadata in decision_metadatas if metadata.get("legacyPhaseRouterPreemption") is True
            ),
            "controllerFullCallCount": sum(
                int(bool(metadata.get("controllerFullCalled"))) for metadata in decision_metadatas
            ),
            "controllerLiteCallCount": sum(
                int(bool(metadata.get("controllerLiteCalled"))) for metadata in decision_metadatas
            ),
            "controllerFullSuccessCount": sum(
                int(bool(metadata.get("controllerFullSucceeded"))) for metadata in decision_metadatas
            ),
            "controllerLiteSuccessCount": sum(
                int(bool(metadata.get("controllerLiteSucceeded"))) for metadata in decision_metadatas
            ),
            "controllerFailureClassCounts": self._controller_failure_class_counts(decision_metadatas),
            "controllerFallbackCount": sum(
                1 for metadata in decision_metadatas if self._is_controller_fallback(metadata)
            ),
            "controllerFallbackWriteCount": sum(
                1
                for metadata in decision_metadatas
                if self._is_controller_fallback(metadata)
                and metadata.get("accepted") is True
                and metadata.get("primaryAction") in {"draft_itinerary", "patch_itinerary", "optimize_route"}
            ),
            "executionRouteMismatchCount": sum(
                1
                for metadata in decision_metadatas
                if self._actual_execution_route_for_decision(metadata, outcome_events)
                and metadata.get("executionOverride") is not True
                and metadata.get("proposedExecutionRoute")
                != self._actual_execution_route_for_decision(metadata, outcome_events)
            ),
            "actionDirectiveModelCount": sum(
                1 for metadata in decision_metadatas if metadata.get("actionDirectiveSource") == "model"
            ),
            "postObservationDecisionCount": sum(
                1 for metadata in decision_metadatas if metadata.get("postObservationDecision") is True
            ),
            "deterministicInitialPlanUsedCount": sum(
                1
                for event in [*(artifact.get("planningSteps") or []), *(artifact.get("toolEvents") or [])]
                if isinstance(event, dict)
                and self._event_result_preview(event).get("deterministicInitialPlanUsed") is True
            ),
            "genericToolLoopWithoutAcceptedDecisionCount": semantic_integrity_metrics.get(
                "genericToolLoopWithoutDecisionCount", 0
            ),
            "verifiedWriteCount": int(
                bool(final_response.get("activeVersionId"))
                and (
                    outcome_metadata.get("verifierPassed") is True
                    or verified_write_event
                    or (verifier_report.get("passed") is True and not bool(verifier_report.get("hardFailures")))
                )
            ),
            "rollbackCount": int(bool(outcome_metadata.get("rollbackPerformed"))),
            "manualChoiceRequiredCount": int(bool(local_options.get("includeCustomOption"))),
            **fallback_confirmation_metrics,
            "ruleSafeDraftExecutionCount": fallback_confirmation_metrics.get("deterministicSafeDraftExecutionCount", 0),
            "structuredChoiceIdentityMismatchCount": self._structured_choice_identity_mismatch_count(
                artifact.get("context") or {}
            ),
            "controllerCallOnRuleSafeConfirmCount": sum(
                1
                for metadata in decision_metadatas
                if ((artifact.get("context") or {}).get("selectedAgentChoice") or {}).get("persistedChoiceAction")
                == "confirm_rule_safe_draft"
                and (metadata.get("controllerFullCalled") or metadata.get("controllerLiteCalled"))
            ),
            "safeFallbackOfferedCount": sum(
                1
                for event in artifact.get("planningSteps") or []
                if isinstance(event, dict) and event.get("type") == "fallback_confirmation_offered"
            ),
            "localPoiOptionCount": len(local_options.get("options") or []),
            "localPoiManualOptionAvailable": bool(local_options.get("includeCustomOption")),
            "localMutationDetected": any(
                isinstance(event, dict) and event.get("type") == "timeline_mutation_detected"
                for event in artifact.get("planningSteps") or []
            ),
            "timelineMutationStatus": timeline_mutation_outcome.get("status"),
            "timelineMutationOperation": timeline_mutation_outcome.get("operation"),
            "boundSegmentIds": list(timeline_mutation_outcome.get("targetSegmentIds") or []),
            "versionDelta": timeline_mutation_metrics["localMutationVersionDelta"],
            "patchDelta": timeline_mutation_metrics["localMutationPatchCount"],
            "postconditionPassed": timeline_mutation_outcome.get("postconditionPassed"),
            "structuralVerifierPassed": timeline_mutation_outcome.get("structuralVerifierPassed"),
            "timelineMutationRollbackPerformed": bool(timeline_mutation_outcome.get("rollbackPerformed")),
            "directChangedSegmentIds": list(timeline_mutation_outcome.get("directChangedSegmentIds") or []),
            "unexpectedChangedSegmentIds": list(timeline_mutation_outcome.get("unexpectedChangedSegmentIds") or []),
            "genericToolLoopEntered": bool((artifact.get("context") or {}).get("genericToolLoopEntered")),
            "falseTimelineMutationSuccessCount": int(
                timeline_mutation_outcome.get("status") != "success"
                and any(
                    isinstance(event, dict) and event.get("type") == "timeline_mutation_committed"
                    for event in artifact.get("planningSteps") or []
                )
            ),
            **react_causal_metrics,
            **timeline_mutation_metrics,
            "dayCount": len(days),
            **self._full_itinerary_signature_metrics(days),
            "routeableAnchorsByDay": routeable_by_day,
            "ordinaryMealPlaceholders": len(ordinary_meals),
            "ordinaryMealExpandable": all(
                self._ordinary_meal_expandable(segment, pending_candidates) for segment in ordinary_meals
            ),
            "explicitMealGrounded": all(
                self._required_meal_grounded_or_pending(segment, pending_candidates) for segment in required_meals
            ),
            **explicit_food,
            **untrusted_meal,
            **route_quality,
            **route_mode_metrics,
            "riskMealSearchCount": self._risk_meal_search_count(snapshot),
            "riskSearchSkipsOrdinaryMeals": self._risk_meal_search_count(snapshot) == 0,
            **web_search_metrics,
            "weakNightViewPoiCount": self._weak_night_view_count(days),
            "nightViewDuplicateFamilyCount": self._night_view_duplicate_family_count(days),
            **self._first_night_view_metrics(days),
            "riskLowWithoutSourceCount": self._risk_low_without_source_count(snapshot),
            "routeQualityStatus": self._route_quality_status(artifact),
            "segmentStartTimes": {
                str(segment.get("id")): str(segment.get("startTime") or "")
                for segment in self._segments(days)
                if segment.get("id")
            },
            "effectiveUserMessage": (artifact.get("context") or {}).get("effectiveUserMessage"),
            "latestUserMessage": (artifact.get("context") or {}).get("latestUserMessage"),
            "resumePlanningAttemptEnabled": bool(
                ((artifact.get("context") or {}).get("resumePlanningAttempt") or {}).get("enabled")
            ),
            "regeneratePlanningRequestEnabled": bool(
                ((artifact.get("context") or {}).get("regeneratePlanningRequest") or {}).get("enabled")
            ),
            "asksForKnownTripBasics": self._asks_for_known_trip_basics(str(final_response.get("assistantReply") or "")),
            "planningPreviewPresent": self._planning_preview_present(artifact),
            "unassignedPoolNoiseCount": self._unassigned_pool_noise_count(artifact),
            "combinedText": self._combined_artifact_text(artifact, final_response),
            **timeline_edit_metrics,
            **route_pair_metrics,
            **route_optimization_metrics,
            **candidate_selection_metrics,
            **meal_cost_metrics,
            **tool_budget_metrics,
            **latency_budget_metrics,
            **creative_metrics,
            **daily_goal_metrics,
            **pool_priority_metrics,
            **food_cardinality_metrics,
            **semantic_integrity_metrics,
            **runtime_debug,
            # Ownership fields are derived from canonical decision/outcome
            # artifacts and must not be overwritten by legacy debug aliases.
            "actualExecutionRoute": initial_actual_execution_route
            or (artifact.get("context") or {}).get("actualExecutionRoute")
            or persisted_response.get("actualExecutionRoute")
            or timeline_mutation_event_metadata.get("actualExecutionRoute"),
            "executionRouteMismatchCount": sum(
                1
                for metadata in decision_metadatas
                if self._actual_execution_route_for_decision(metadata, outcome_events)
                and metadata.get("executionOverride") is not True
                and metadata.get("proposedExecutionRoute")
                != self._actual_execution_route_for_decision(metadata, outcome_events)
            ),
            "controllerFallbackCount": sum(
                1 for metadata in decision_metadatas if self._is_controller_fallback(metadata)
            ),
            "controllerFallbackWriteCount": sum(
                1
                for metadata in decision_metadatas
                if self._is_controller_fallback(metadata)
                and metadata.get("accepted") is True
                and metadata.get("primaryAction") in {"draft_itinerary", "patch_itinerary", "optimize_route"}
            ),
            "plannerCalledOnHealthyControllerCount": sum(
                1
                for metadata in decision_metadatas
                if metadata.get("source") == "controller" and metadata.get("plannerCalled") is True
            ),
            "timelineParserPreDecisionCount": sum(
                1 for metadata in decision_metadatas if metadata.get("timelineParserCalledBeforeDecision") is True
            ),
            "legacyPhaseRouterPreemptionCount": sum(
                1 for metadata in decision_metadatas if metadata.get("legacyPhaseRouterPreemption") is True
            ),
            "amapPoiQueryKeys": list(runtime_debug.get("mealSearchQueries") or []),
            "verifierPassed": bool(verified_write_event)
            or outcome_metadata.get("verifierPassed") is True
            or (verifier_report.get("passed") is not False and not bool(verifier_report.get("hardFailures"))),
        }

    def _daily_goal_convergence_metrics(
        self,
        artifact: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, int]:
        """Derive grounded occurrence and local-insertion evidence from persisted artifacts."""
        proposal_rows = [item for item in artifact.get("planProposals") or [] if isinstance(item, dict)]
        proposal_snapshots = [
            proposal.get("snapshotJson") or proposal.get("snapshot_json")
            for proposal in proposal_rows
            if isinstance(proposal.get("snapshotJson") or proposal.get("snapshot_json"), dict)
        ]
        portfolio = artifact.get("portfolio") if isinstance(artifact.get("portfolio"), dict) else {}
        selection = artifact.get("portfolioSelection") if isinstance(artifact.get("portfolioSelection"), dict) else {}
        selected_proposal_id = str(
            selection.get("selectedProposalId")
            or selection.get("selected_proposal_id")
            or portfolio.get("selectedProposalId")
            or portfolio.get("selected_proposal_id")
            or ""
        )
        portfolio_status = str(portfolio.get("status") or selection.get("status") or "")
        active_version_id = str(
            ((artifact.get("sessionSnapshot") or {}).get("conversation_session") or {}).get("active_version_id")
            or selection.get("activeVersionId")
            or selection.get("active_version_id")
            or selection.get("resultVersionId")
            or selection.get("result_version_id")
            or ""
        )
        proposal_stage = bool(proposal_snapshots) and (
            portfolio_status == "awaiting_selection" or not selected_proposal_id or not active_version_id
        )
        if proposal_stage:
            evaluation_snapshots = proposal_snapshots
        elif isinstance(snapshot.get("days"), list) and snapshot.get("days"):
            evaluation_snapshots = [snapshot]
        elif proposal_snapshots:
            evaluation_snapshots = proposal_snapshots
        else:
            evaluation_snapshots = [snapshot]

        def occurrence_evidence(
            candidate: dict[str, Any],
        ) -> tuple[list[dict[str, Any]], list[tuple[int, dict[str, Any], dict[str, Any]]]]:
            raw_plan = candidate.get("portfolioGoalOccurrencePlan")
            if not isinstance(raw_plan, dict):
                raw_plan = (artifact.get("context") or {}).get("goalOccurrencePlan")
            items = raw_plan.get("occurrences") if isinstance(raw_plan, dict) else []
            items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
            days = candidate.get("days") if isinstance(candidate.get("days"), list) else []
            selected = [
                (int(day.get("dayNumber") or 0), segment, segment.get("semanticMetadata") or {})
                for day in days
                if isinstance(day, dict)
                for segment in (day.get("segments") or [])
                if isinstance(segment, dict) and isinstance(segment.get("semanticMetadata"), dict)
            ]
            return items, selected

        target_city = str(
            (artifact.get("context") or {}).get("selectedCity")
            or (artifact.get("context") or {}).get("city")
            or snapshot.get("city")
            or ""
        ).strip()

        def normalized_city(value: Any) -> str:
            return str(value or "").strip().removesuffix("市")

        def grounded(segment: dict[str, Any], metadata: dict[str, Any]) -> bool:
            poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
            status = str(metadata.get("groundingStatus") or poi.get("groundingStatus") or "selected")
            source = str(poi.get("source") or "").lower()
            return bool(
                poi.get("amapId")
                and poi.get("city")
                and (not target_city or normalized_city(poi.get("city")) == normalized_city(target_city))
                and source == AMAP_PLACE_SOURCE
                and not self.poi_trust_policy.is_mock_or_synthetic_poi_values(
                    source=poi.get("source"),
                    amap_id=poi.get("amapId"),
                    source_note=poi.get("sourceNote"),
                    name=poi.get("name"),
                    kind=segment.get("kind"),
                    intent_type=metadata.get("intentType"),
                )
                and status not in {"waiting_for_poi_grounding", "pending", "unresolved", "rejected"}
            )

        occurrence_sets = [occurrence_evidence(candidate) for candidate in evaluation_snapshots]
        occurrences, segments = occurrence_sets[0]
        expected = {str(item.get("occurrenceId") or "") for item in occurrences if item.get("occurrenceId")}
        actual_sets = [
            {
                str(metadata.get("occurrenceId") or "")
                for _, segment, metadata in selected
                if metadata.get("occurrenceId") and grounded(segment, metadata)
            }
            for _, selected in occurrence_sets
        ]
        expected_day_goal = {
            (int(item.get("dayNumber") or 0), str(item.get("sourceGoalId") or ""))
            for item in occurrences
            if item.get("sourceGoalId")
        }
        actual_day_goal_sets = [
            {
                (day_number, str(metadata.get("sourceGoalId") or metadata.get("goalId") or ""))
                for day_number, segment, metadata in selected
                if grounded(segment, metadata) and (metadata.get("sourceGoalId") or metadata.get("goalId"))
            }
            for _, selected in occurrence_sets
        ]

        def distinct_violations(
            items: list[dict[str, Any]], selected: list[tuple[int, dict[str, Any], dict[str, Any]]]
        ) -> int:
            identities = {
                str(metadata.get("occurrenceId") or ""): str((segment.get("poi") or {}).get("amapId") or "")
                for _, segment, metadata in selected
                if metadata.get("occurrenceId") and grounded(segment, metadata)
            }
            groups: dict[str, list[str]] = {}
            for occurrence in items:
                group = str(occurrence.get("distinctGroupId") or "")
                if group:
                    groups.setdefault(group, []).append(identities.get(str(occurrence.get("occurrenceId") or ""), ""))
            return sum(
                1
                for values in groups.values()
                if len(values) > 1 and (not all(values) or len(set(values)) != len(values))
            )

        explicit_meal = {
            str(item.get("occurrenceId") or "")
            for item in occurrences
            if item.get("intentType") == "meal" and item.get("requirementLevel") == "explicit_soft"
        }
        evidence_missing = max(
            (
                sum(
                    1
                    for _, segment, metadata in selected
                    if metadata.get("occurrenceId")
                    and (
                        not metadata.get("goalId")
                        or not metadata.get("planningSlotId")
                        or not grounded(segment, metadata)
                    )
                )
                for _, selected in occurrence_sets
            ),
            default=0,
        )
        selected_proposal = next(
            (proposal for proposal in proposal_rows if str(proposal.get("id") or "") == selected_proposal_id),
            None,
        )
        selected_snapshot = (
            (selected_proposal.get("snapshotJson") or selected_proposal.get("snapshot_json"))
            if isinstance(selected_proposal, dict)
            else None
        )
        proposal_final_occurrence_mismatch = 0
        proposal_final_identity_mismatch = 0
        if not proposal_stage and isinstance(selected_snapshot, dict) and isinstance(snapshot.get("days"), list):
            proposal_occurrences, proposal_segments = occurrence_evidence(selected_snapshot)
            final_occurrences, final_segments = occurrence_evidence(snapshot)
            proposal_map = {
                str(metadata.get("occurrenceId") or ""): str((segment.get("poi") or {}).get("amapId") or "")
                for _, segment, metadata in proposal_segments
                if metadata.get("occurrenceId") and grounded(segment, metadata)
            }
            final_map = {
                str(metadata.get("occurrenceId") or ""): str((segment.get("poi") or {}).get("amapId") or "")
                for _, segment, metadata in final_segments
                if metadata.get("occurrenceId") and grounded(segment, metadata)
            }
            proposal_expected = {
                str(item.get("occurrenceId") or "") for item in proposal_occurrences if item.get("occurrenceId")
            }
            final_expected = {
                str(item.get("occurrenceId") or "") for item in final_occurrences if item.get("occurrenceId")
            }
            proposal_final_occurrence_mismatch = len(proposal_expected.symmetric_difference(final_expected))
            proposal_final_identity_mismatch = sum(
                1
                for occurrence_id in proposal_expected & final_expected
                if proposal_map.get(occurrence_id) != final_map.get(occurrence_id)
            )

        events = [
            item
            for item in [*(artifact.get("planningSteps") or []), *(artifact.get("toolEvents") or [])]
            if isinstance(item, dict)
        ]
        previews = [
            item.get("metadata", {}).get("resultPreview", item.get("metadata", {}))
            for item in events
            if isinstance(item.get("metadata"), dict)
        ]
        previews = [item for item in previews if isinstance(item, dict)]
        mutation = next(
            (
                item
                for item in previews
                if isinstance(item.get("mutationPreflight"), dict) or item.get("adjacentSearchCenter")
            ),
            {},
        )
        outcome = next(
            (
                item.get("timelineMutationOutcome")
                for item in previews
                if isinstance(item.get("timelineMutationOutcome"), dict)
            ),
            {},
        )
        preflight = mutation.get("mutationPreflight") if isinstance(mutation.get("mutationPreflight"), dict) else {}
        return {
            "proposalStageEvidenceUsed": int(proposal_stage),
            "goalOccurrencePlanCount": int(bool(occurrences)),
            "goalOccurrenceExpectedCount": len(expected),
            "goalOccurrenceActualCount": min(
                (len(expected & candidate_actual) for candidate_actual in actual_sets), default=0
            ),
            "goalOccurrenceCoverageFailureCount": max(
                (len(expected - candidate_actual) for candidate_actual in actual_sets), default=len(expected)
            ),
            "dailyGoalCoverageFailureCount": max(
                (len(expected_day_goal - candidate_actual) for candidate_actual in actual_day_goal_sets),
                default=len(expected_day_goal),
            ),
            "distinctAcrossDaysViolationCount": max(
                (distinct_violations(items, selected) for items, selected in occurrence_sets), default=0
            ),
            "groundedExplicitMealOccurrenceExpectedCount": len(explicit_meal),
            "groundedExplicitMealOccurrenceActualCount": min(
                (len(explicit_meal & candidate_actual) for candidate_actual in actual_sets), default=0
            ),
            "backendRegexOccurrenceDecisionCount": sum(
                1 for item in occurrences if str(item.get("source") or "").lower().startswith("regex")
            ),
            "controllerDayStrategyOccurrenceCount": sum(
                1 for item in occurrences if item.get("source") == "controller_day_strategy"
            ),
            "occurrenceEvidenceMissingCount": evidence_missing,
            "selectedProposalFinalOccurrenceMismatchCount": proposal_final_occurrence_mismatch,
            "selectedProposalFinalIdentityMismatchCount": proposal_final_identity_mismatch,
            "adjacentSearchCenterCount": sum(1 for item in previews if item.get("adjacentSearchCenter")),
            "genericAddGlobalSearchBeforeAdjacentCount": int(bool(mutation.get("globalSearchBeforeAdjacent"))),
            "insertionRouteVerifiedCandidateCount": sum(
                1 for item in previews if (item.get("routeVerification") or {}).get("status") == "verified"
            ),
            "mutationPreflightWriteAttemptCount": int(preflight.get("writeAttemptCount") or 0),
            "mutationPreflightVersionDelta": int(preflight.get("versionDelta") or 0),
            "mutationPreflightPatchDelta": int(preflight.get("patchDelta") or 0),
            "localAddSelectedCandidateCount": int(bool(outcome.get("selectedAmapPoiId") or outcome.get("amapPoiId"))),
            "localAddVersionDelta": int(outcome.get("versionDelta") or 0),
            "localAddPatchDelta": int(outcome.get("patchDelta") or 0),
            "localAddRollbackCount": int(outcome.get("rollbackCount") or 0),
            "farOnlyCandidateZeroWriteCount": int(
                str(outcome.get("failureReason") or "") == "no_adjacent_route_feasible_candidate"
                and int(outcome.get("versionDelta") or 0) == 0
                and int(outcome.get("patchDelta") or 0) == 0
            ),
            "exactEntitySubstitutionCount": int(
                str(outcome.get("failureReason") or "") == "exact_entity_identity_mismatch"
            ),
        }

    def _react_causal_integrity_metrics(
        self,
        artifact: dict[str, Any],
        final_response: dict[str, Any],
        decision_metadatas: list[dict[str, Any]],
        outcome_events: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Derive ReAct ownership evidence; never trust self-reported counters."""

        events = [
            item
            for item in [*(artifact.get("planningSteps") or []), *(artifact.get("toolEvents") or [])]
            if isinstance(item, dict)
        ]
        action_types = {
            "timeline_edit",
            "timeline_mutation_detected",
            "timeline_patch_started",
            "timeline_mutation_committed",
            "patch_itinerary",
            "staged_pipeline_started",
        }
        first_decision_index = next(
            (
                index
                for index, item in enumerate(events)
                if item.get("type") in {"agent_decision", "agent_control_cycle"}
            ),
            None,
        )
        first_action_index = next(
            (index for index, item in enumerate(events) if str(item.get("type") or "") in action_types),
            None,
        )
        pre_controller_router_count = int(
            first_action_index is not None
            and (first_decision_index is None or first_action_index < first_decision_index)
        )
        accepted_model_patch = any(
            item.get("source") == "controller"
            and item.get("accepted") is True
            and item.get("primaryAction") == "patch_itinerary"
            for item in decision_metadatas
        )
        write_like_event = any(str(item.get("type") or "") in action_types for item in events)
        local_mutation_bypass_count = int(write_like_event and not accepted_model_patch)

        snapshot = artifact.get("sessionSnapshot") if isinstance(artifact.get("sessionSnapshot"), dict) else {}
        transactions = snapshot.get("timeline_mutation_transactions") or []
        patches = snapshot.get("itinerary_patches") or []
        versions = snapshot.get("itinerary_versions") or []
        persisted_turns = snapshot.get("conversation_turns") or []
        persisted_success = any(
            isinstance(turn, dict)
            and isinstance(turn.get("agent_response_json"), dict)
            and ((turn.get("agent_response_json") or {}).get("timelineMutationOutcome") or {}).get("status")
            == "success"
            for turn in persisted_turns
        )
        reply = str(final_response.get("reply") or final_response.get("assistantReply") or "")
        success_claim = persisted_success or bool(
            re.search(r"(?:^|[，。；\s])(已|成功|完成)(?:将|把|修改|调整|保存)", reply)
        )
        successful_write_previews = [
            self._event_result_preview(item)
            for item in outcome_events
            if isinstance(item, dict)
            and self._event_result_preview(item).get("status") == "success"
            and bool(self._event_result_preview(item).get("resultVersionId"))
            and bool(self._event_result_preview(item).get("patchIds"))
            and (
                self._event_result_preview(item).get("verifierPassed") is True
                or (self._event_result_preview(item).get("verifier") or {}).get("passed") is True
            )
        ]
        verified_success_outcome = bool(successful_write_previews)
        verified_candidate_transaction = any(
            preview.get("action") == "patch_itinerary"
            and preview.get("executionRoute") == "timeline_mutation_executor"
            and bool((preview.get("candidateSummary") or {}).get("candidateRecordId"))
            and bool((preview.get("candidateSummary") or {}).get("amapPoiId"))
            and len(preview.get("changedSegmentIds") or []) == 1
            and {
                "candidate_patch_started",
                "candidate_patch_applied",
                "candidate_patch_verifier",
                "candidate_patch_committed",
            }.issubset(
                {
                    str(event.get("name") or "")
                    for event in preview.get("executionEvents") or []
                    if isinstance(event, dict)
                }
            )
            for preview in successful_write_previews
        )
        transaction_evidence_missing = int(
            success_claim and not (patches and versions and (transactions or verified_candidate_transaction))
        )
        false_success_claim_count = int(
            success_claim and (transaction_evidence_missing or not verified_success_outcome)
        )

        duration_failure = 0
        for event in events:
            preview = self._event_result_preview(event)
            before = preview.get("before") if isinstance(preview.get("before"), dict) else {}
            after = preview.get("after") if isinstance(preview.get("after"), dict) else {}
            before_duration = before.get("durationMinutes")
            after_duration = after.get("durationMinutes")
            if (
                isinstance(before_duration, (int, float))
                and isinstance(after_duration, (int, float))
                and before_duration != after_duration
            ):
                duration_failure += 1
        return {
            "preControllerDomainRouterCount": pre_controller_router_count,
            "localMutationBypassCount": local_mutation_bypass_count,
            "falseSuccessClaimCount": false_success_claim_count,
            "transactionEvidenceMissingCount": transaction_evidence_missing,
            "durationPreservationFailureCount": duration_failure,
        }

    @staticmethod
    def _is_controller_fallback(decision: dict[str, Any]) -> bool:
        decision_path = str(decision.get("decisionPath") or "")
        if decision_path:
            return decision_path == "fallback"
        return bool(decision.get("controllerError"))

    def _actual_execution_route_for_decision(
        self,
        decision: dict[str, Any],
        outcome_events: list[dict[str, Any]],
    ) -> Optional[str]:
        action = str(decision.get("primaryAction") or "")
        cycle_index = decision.get("cycleIndex")
        for event in outcome_events:
            if not isinstance(event, dict):
                continue
            preview = self._event_result_preview(event)
            if str(preview.get("action") or "") != action:
                continue
            if cycle_index is not None and preview.get("cycleIndex") != cycle_index:
                continue
            route = str(preview.get("executionRoute") or "")
            if route:
                return route
        return None

    @staticmethod
    def _controller_failure_class_counts(decisions: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for decision in decisions:
            for failure in decision.get("controllerFailures") or []:
                if not isinstance(failure, dict):
                    continue
                failure_class = str(failure.get("failureClass") or "")
                if failure_class:
                    counts[failure_class] = counts.get(failure_class, 0) + 1
        return counts

    @staticmethod
    def _structured_choice_identity_mismatch_count(context: dict[str, Any]) -> int:
        selected = context.get("selectedAgentChoice") if isinstance(context.get("selectedAgentChoice"), dict) else {}
        if not selected:
            return 0
        ids = [str(selected.get(key) or "") for key in ("requestChoiceId", "persistedChoiceId", "executionChoiceId")]
        actions = [str(selected.get(key) or "") for key in ("persistedChoiceAction", "executionAction")]
        return int((bool(all(ids)) and len(set(ids)) != 1) or (bool(all(actions)) and len(set(actions)) != 1))

    def _selected_choice_for_eval_turn(
        self,
        session_id: Optional[str],
        turn: dict[str, Any],
        selected_choices: dict[int, dict[str, Any]],
        turn_results: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        exact_source_turn = turn.get("selectedAgentChoiceExactIdFromTurn")
        if exact_source_turn is not None:
            source_index = int(exact_source_turn)
            if source_index < 1 or source_index > len(turn_results):
                raise ValueError(f"exact choice source turn is unavailable: {source_index}")
            prior = turn_results[source_index - 1]
            snapshot = prior.get("sessionSnapshot") if isinstance(prior.get("sessionSnapshot"), dict) else {}
            turns = snapshot.get("conversation_turns") if isinstance(snapshot.get("conversation_turns"), list) else []
            assistant = next(
                (
                    item
                    for item in reversed(turns)
                    if isinstance(item, dict)
                    and item.get("role") == "assistant"
                    and isinstance(item.get("agent_response_json"), dict)
                    and item["agent_response_json"].get("choiceOptions")
                ),
                {},
            )
            response = (
                assistant.get("agent_response_json") if isinstance(assistant.get("agent_response_json"), dict) else {}
            )
            options = response.get("choiceOptions") if isinstance(response.get("choiceOptions"), list) else []
            option_index = int(turn.get("selectedAgentChoiceOptionIndex") or 1)
            if option_index < 1 or option_index > len(options):
                raise ValueError(f"exact choice option index is unavailable: {option_index}")
            option = options[option_index - 1]
            if not isinstance(option, dict) or not option.get("id"):
                raise ValueError("prior artifact option has no exact id")
            choice = {
                "sourceAssistantTurnId": str(assistant.get("id") or ""),
                "choiceId": str(option["id"]),
            }
            manual_value = turn.get("manualValue")
            if manual_value is not None:
                choice["manualValue"] = str(manual_value)
            return choice
        source_turn = turn.get("selectedAgentChoiceFromTurn")
        if source_turn is not None:
            return dict(selected_choices.get(int(source_turn)) or {}) or None
        action = str(turn.get("selectedAgentChoiceAction") or "").strip()
        if not action or not session_id:
            return None
        session = ConversationService(self.runtime.db).get_session(session_id)
        for assistant_turn in reversed(session.turns):
            if assistant_turn.role != "assistant":
                continue
            for option in assistant_turn.choice_options:
                if str(option.get("action") or "") != action:
                    continue
                choice = {
                    "sourceAssistantTurnId": assistant_turn.id,
                    "choiceId": str(option.get("id") or ""),
                }
                manual_value = turn.get("manualValue")
                if manual_value is not None:
                    choice["manualValue"] = str(manual_value)
                return choice
        raise ValueError(f"no persisted choice option found for action: {action}")

    def _food_experience_cardinality_metrics(self, artifact: dict[str, Any]) -> dict[str, int]:
        context = artifact.get("context") if isinstance(artifact.get("context"), dict) else {}
        contract = (
            context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
        )
        goal = contract.get("foodExperienceGoal") if isinstance(contract.get("foodExperienceGoal"), dict) else {}
        required_intents = contract.get("requiredIntents") if isinstance(contract.get("requiredIntents"), list) else []
        meal_target = sum(
            self._safe_int(item.get("requiredCount") or item.get("target") or 0)
            for item in required_intents
            if isinstance(item, dict) and str(item.get("intentType") or "") == "meal"
        )
        return {
            "foodExperienceMinCount": self._safe_int(goal.get("minCount")),
            "foodExperiencePreferredCount": self._safe_int(goal.get("preferredCount")),
            "foodExperienceMaxCount": self._safe_int(goal.get("maxCount")),
            "requiredMealTargetCount": meal_target,
        }

    def _semantic_integrity_metrics(
        self,
        artifact: dict[str, Any],
        *,
        persisted_response: dict[str, Any],
    ) -> dict[str, Any]:
        pool_reports: list[dict[str, Any]] = []
        for event in [*(artifact.get("planningSteps") or []), *(artifact.get("toolEvents") or [])]:
            if not isinstance(event, dict):
                continue
            preview = self._event_result_preview(event)
            pool_reports.extend(report for report in preview.get("poolReports") or [] if isinstance(report, dict))
        museum_gates = [
            gate
            for report in pool_reports
            if str(report.get("intentType") or "") == "museum"
            for gate in report.get("semanticGateResults") or []
            if isinstance(gate, dict)
        ]
        selected_museum_names = {
            str(name)
            for report in pool_reports
            if str(report.get("intentType") or "") == "museum"
            for name in report.get("selectedCanonicalEntities") or []
            if str(name)
        }
        selected_invalid = [
            gate
            for gate in museum_gates
            if gate.get("passed") is False and str(gate.get("candidateName") or "") in selected_museum_names
        ]
        proof = (
            persisted_response.get("readOnlySideEffectProof")
            if isinstance(persisted_response.get("readOnlySideEffectProof"), dict)
            else {}
        )
        timeline_result = (
            persisted_response.get("timelineQueryResult")
            if isinstance(persisted_response.get("timelineQueryResult"), dict)
            else {}
        )
        observations = [item for item in artifact.get("agentObservations") or [] if isinstance(item, dict)]
        final_coverage = (
            observations[-1].get("requirementCoverage", {}).get("required", [])
            if observations and isinstance(observations[-1].get("requirementCoverage"), dict)
            else []
        )
        invalid_claims = [
            claim
            for item in final_coverage
            if isinstance(item, dict)
            for claim in item.get("invalidClaims") or []
            if isinstance(claim, dict)
        ]
        decision_present = any(
            isinstance(item, dict) and item.get("type") == "agent_decision"
            for item in artifact.get("planningSteps") or []
        )
        tool_loop_entered = bool(self._tool_budget_metrics(artifact).get("toolLoopEntered"))
        return {
            "museumSemanticCandidateCount": len(museum_gates),
            "museumSemanticValidCandidateCount": sum(1 for item in museum_gates if item.get("passed") is True),
            "museumSemanticRejectedCandidateCount": sum(1 for item in museum_gates if item.get("passed") is False),
            "museumSemanticMismatchSelectedCount": len(selected_invalid),
            "museumTimelineInvalidClaimCount": len(timeline_result.get("invalidClaims") or []),
            "readOnlyExternalCallCount": self._safe_int(proof.get("externalCalls")),
            "readOnlyPatchCount": self._safe_int(proof.get("patchCount")),
            "readOnlyVersionDelta": self._safe_int(proof.get("versionDelta")),
            "readOnlyUnchangedVerifierPassed": proof.get("unchangedVerifierPassed"),
            "genericToolLoopWithoutDecisionCount": int(tool_loop_entered and not decision_present),
            "controllerFallbackRoute": (
                str((artifact.get("agentDecision") or {}).get("gatedDecision", {}).get("source") or "")
                if isinstance(artifact.get("agentDecision"), dict)
                else ""
            ),
            "goalClaimMismatchCount": len(invalid_claims),
        }

    def _creative_variant_metrics(self, artifact: dict[str, Any], final_response: dict[str, Any]) -> dict[str, Any]:
        combined_text = self._combined_artifact_text(artifact, final_response)
        creative_variant_ids: set[str] = set()
        for source in [
            (artifact.get("context") or {}).get("creativeVariant"),
            *[event.get("metadata") for event in (artifact.get("planningSteps") or []) if isinstance(event, dict)],
            *[event.get("metadata") for event in (artifact.get("toolEvents") or []) if isinstance(event, dict)],
        ]:
            if not isinstance(source, dict):
                continue
            direct = source.get("creativeVariantId")
            if direct:
                creative_variant_ids.add(str(direct))
            preview = source.get("resultPreview") if isinstance(source.get("resultPreview"), dict) else {}
            for report in preview.get("poolReports") or []:
                if isinstance(report, dict) and report.get("creativeVariantId"):
                    creative_variant_ids.add(str(report["creativeVariantId"]))
        snapshot_names = self._snapshot_poi_names(artifact.get("itinerarySnapshot") or {})
        previous_names = self._snapshot_poi_names(
            (artifact.get("context") or {}).get("previousItinerarySnapshot") or {}
        )
        repeated_names = snapshot_names & previous_names
        return {
            "creativeStyleDeclared": bool(
                re.search(
                    r"(这版按|本版.*风格|创意|CityWalk|本地沉浸|美食主线|文化深游|自然松弛|摄影夜游|亲子轻松)",
                    combined_text,
                )
            ),
            "creativeVariantPresent": bool(creative_variant_ids),
            "creativeVariantIds": sorted(creative_variant_ids),
            "repeatedPoiNameCount": len(repeated_names),
            "repeatedPoiNames": sorted(repeated_names),
        }

    def _intent_pool_priority_metrics(self, artifact: dict[str, Any]) -> dict[str, Any]:
        reports: list[dict[str, Any]] = []
        for event in [*(artifact.get("planningSteps") or []), *(artifact.get("toolEvents") or [])]:
            if not isinstance(event, dict):
                continue
            preview = self._event_result_preview(event)
            for report in preview.get("poolReports") or []:
                if isinstance(report, dict):
                    reports.append(report)
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for report in reports:
            key = (str(report.get("poolId") or ""), str(report.get("intentType") or ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(report)
        coverage = {
            str(report.get("intentType") or report.get("poolId") or ""): str(report.get("coverageStatus") or "")
            for report in deduped
            if report.get("intentType") or report.get("poolId")
        }
        priority_classes = [str(report.get("priorityClass") or "") for report in deduped]
        first_optional = next(
            (index for index, value in enumerate(priority_classes) if value == "optional_creative"), None
        )
        last_required = max(
            (index for index, value in enumerate(priority_classes) if value.startswith("required_")),
            default=-1,
        )
        return {
            "intentPoolCoverage": coverage,
            "intentPoolPriorityClasses": priority_classes,
            "requiredPoolsPrecedeOptional": first_optional is None or last_required < first_optional,
        }

    @staticmethod
    def _decision_event_before_action(events: list[dict[str, Any]]) -> bool:
        decision_index = next(
            (index for index, item in enumerate(events) if item.get("type") == "agent_decision"), None
        )
        action_index = next(
            (index for index, item in enumerate(events) if item.get("type") == "agent_action_started"),
            None,
        )
        return decision_index is not None and action_index is not None and decision_index < action_index

    def _snapshot_poi_names(self, snapshot: dict[str, Any]) -> set[str]:
        days = snapshot.get("days") if isinstance(snapshot.get("days"), list) else []
        names: set[str] = set()
        for segment in self._segments(days):
            poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
            name = re.sub(r"[\s\-_,.()（）·・，。]+", "", str(poi.get("name") or ""))
            if name:
                names.add(name)
        return names

    def _full_itinerary_signature_metrics(self, days: list[dict[str, Any]]) -> dict[str, Any]:
        ordered_entities: list[str] = []
        meal_brands: list[str] = []
        for day in sorted(days, key=lambda item: int(item.get("dayNumber") or 0)):
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                raw_name = str(poi.get("name") or segment.get("title") or "")
                canonical_name = re.sub(r"[\s\-_,.()（）·・，。]+", "", raw_name).casefold()
                kind = str(segment.get("kind") or "visit").casefold()
                if canonical_name:
                    ordered_entities.append(f"{kind}:{canonical_name}")
                    if kind == "meal":
                        meal_brands.append(canonical_name)
        signature_source = "|".join(ordered_entities)
        duplicate_meal_brands = len(meal_brands) - len(set(meal_brands))
        return {
            "fullItinerarySignature": hashlib.sha256(signature_source.encode("utf-8")).hexdigest()[:16]
            if signature_source
            else "",
            "fullItineraryEntityCount": len(ordered_entities),
            "duplicateMealBrandCount": duplicate_meal_brands,
        }

    def _timeline_edit_metrics(self, artifact: dict[str, Any]) -> dict[str, Any]:
        snapshot = artifact.get("itinerarySnapshot") or {}
        context = artifact.get("context") if isinstance(artifact.get("context"), dict) else {}
        expectations = (
            context.get("timelinePrecisionExpectations")
            if isinstance(context.get("timelinePrecisionExpectations"), dict)
            else {}
        )
        events = []
        for key in ("planningSteps", "toolEvents"):
            value = artifact.get(key)
            if isinstance(value, list):
                events.extend(item for item in value if isinstance(item, dict))
        timeline_events = [
            item for item in events if item.get("type") == "timeline_edit" or item.get("toolName") == "timeline_edit"
        ]
        preview = self._event_result_preview(timeline_events[-1]) if timeline_events else {}
        command = preview.get("timelineCommand") if isinstance(preview.get("timelineCommand"), dict) else {}
        changed_segment_ids = [str(item) for item in preview.get("changedSegmentIds") or [] if str(item)]
        changed_segments = [
            segment
            for segment in self._segments(snapshot.get("days") if isinstance(snapshot.get("days"), list) else [])
            if str(segment.get("id") or "") in changed_segment_ids
        ]
        changed_meal = next((segment for segment in changed_segments if segment.get("kind") == "meal"), {})
        changed_meal_poi = changed_meal.get("poi") if isinstance(changed_meal.get("poi"), dict) else {}
        target_day_number = int(command.get("dayNumber") or expectations.get("targetDayNumber") or 2)
        target_day = self._day_by_number(snapshot, target_day_number)
        dinner_count = 0
        if target_day:
            for segment in target_day.get("segments") or []:
                if not isinstance(segment, dict) or segment.get("kind") != "meal":
                    continue
                if self._segment_start_minutes(segment) >= 17 * 60:
                    dinner_count += 1
        unchanged_match = True
        unchanged = (
            expectations.get("unchangedDaySegmentIds")
            if isinstance(expectations.get("unchangedDaySegmentIds"), dict)
            else {}
        )
        for day_number, expected_ids in unchanged.items():
            day = self._day_by_number(snapshot, int(day_number))
            actual_ids = [
                str(segment.get("id")) for segment in (day or {}).get("segments") or [] if isinstance(segment, dict)
            ]
            if actual_ids != [str(item) for item in expected_ids]:
                unchanged_match = False
        execution_previews = [self._event_result_preview(event) for event in events]
        if any(item.get("toolLoopEntered") is False for item in execution_previews):
            generic_tool_loop_entered = False
        elif any(item.get("toolLoopEntered") is True for item in execution_previews):
            generic_tool_loop_entered = True
        else:
            generic_tool_loop_entered = any(event.get("type") == "tool" for event in events)
        return {
            "timelineEditEventPresent": bool(timeline_events),
            "timelineEditOperation": command.get("operation"),
            "timelineEditScope": command.get("scope"),
            "timelineEditChangedScope": preview.get("changedScope"),
            "timelineEditChangedSegmentIds": changed_segment_ids,
            "timelineEditChangedSegmentCount": len(changed_segment_ids),
            "timelineEditChangedMealPoiName": str(changed_meal_poi.get("name") or ""),
            "timelineEditChangedMealPoiCategory": str(changed_meal_poi.get("category") or ""),
            "timelineEditGlobalReorder": bool(preview.get("globalReorder")),
            "deterministicTimelineCommand": bool(timeline_events)
            and (preview.get("toolLoopEntered") is False or not any(event.get("type") == "tool" for event in events)),
            "toolLoopEntered": generic_tool_loop_entered,
            "targetDayDinnerCount": dinner_count,
            "timelineEditUnchangedDaysMatch": unchanged_match,
        }

    @staticmethod
    def _timeline_mutation_transaction_metrics(
        artifact: dict[str, Any],
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = artifact.get("sessionSnapshot") if isinstance(artifact.get("sessionSnapshot"), dict) else {}
        mutation_id = str(outcome.get("mutationId") or "")
        transactions = [
            item
            for item in snapshot.get("timeline_mutation_transactions") or []
            if mutation_id and isinstance(item, dict) and str(item.get("mutation_id") or "") == mutation_id
        ]
        patches = [
            item
            for item in snapshot.get("itinerary_patches") or []
            if mutation_id and isinstance(item, dict) and str(item.get("mutation_id") or "") == mutation_id
        ]
        patch_ids = {str(item.get("id") or "") for item in patches if item.get("id")}
        versions = [
            item
            for item in snapshot.get("itinerary_versions") or []
            if isinstance(item, dict) and str(item.get("source_patch_id") or "") in patch_ids
        ]
        transaction_payload = (
            transactions[-1].get("transaction_json")
            if transactions and isinstance(transactions[-1].get("transaction_json"), dict)
            else {}
        )
        result_version_id = str(outcome.get("resultVersionId") or transaction_payload.get("resultVersionId") or "")
        active_version_id = str((snapshot.get("conversation_session") or {}).get("active_version_id") or "")
        orphan_versions = [
            item
            for item in versions
            if str(item.get("id") or "") != result_version_id
            or (outcome.get("status") == "success" and result_version_id != active_version_id)
        ]
        events_by_identity: dict[str, dict[str, Any]] = {}
        for key in ("planningSteps", "toolEvents"):
            for item in artifact.get(key) or []:
                if isinstance(item, dict):
                    events_by_identity.setdefault(
                        json.dumps(item, ensure_ascii=False, sort_keys=True, default=str), item
                    )
        events = list(events_by_identity.values())
        event_types = [str(item.get("type") or "") for item in events]
        unrelated_markers = ("web_search", "amap_weather", "ticket_lookup", "poi_risk", "risk_search")
        unrelated_external_calls = sum(
            1
            for item in events
            if any(
                marker in " ".join(str(item.get(key) or "") for key in ("type", "label", "toolName"))
                for marker in unrelated_markers
            )
        )
        context = artifact.get("context") if isinstance(artifact.get("context"), dict) else {}
        before_after = (
            transaction_payload.get("beforeAfterDiff")
            if isinstance(transaction_payload, dict) and isinstance(transaction_payload.get("beforeAfterDiff"), dict)
            else {}
        )
        postcondition = (
            transaction_payload.get("postconditionVerifier")
            if isinstance(transaction_payload, dict)
            and isinstance(transaction_payload.get("postconditionVerifier"), dict)
            else {}
        )
        touched_pairs = outcome.get("touchedRoutePairs") or (
            transaction_payload.get("routeRefreshScope", {}).get("pairs")
            if isinstance(transaction_payload.get("routeRefreshScope"), dict)
            else []
        )
        return {
            "localMutationDetectedCount": event_types.count("timeline_mutation_detected"),
            "localMutationBoundCount": event_types.count("timeline_target_bound"),
            "localMutationAmbiguousCount": event_types.count("timeline_target_ambiguous"),
            "localMutationTargetNotFoundCount": event_types.count("timeline_target_not_found"),
            "localMutationControllerBypassCount": int(context.get("controllerBypassed") is True),
            "localMutationGenericToolLoopCount": int(context.get("genericToolLoopEntered") is True),
            "localMutationUnrelatedExternalCallCount": unrelated_external_calls,
            "localMutationPatchCount": len(patches),
            "localMutationVersionDelta": len(versions),
            "localMutationPostconditionPassCount": int(postcondition.get("passed") is True),
            "localMutationRollbackCount": int(outcome.get("rollbackPerformed") is True),
            "localMutationNoOpCount": int(outcome.get("status") == "no_change"),
            "localMutationMisleadingSuccessCount": int(
                outcome.get("status") != "success" and "timeline_mutation_committed" in event_types
            ),
            "localMutationUnexpectedChangedSegmentCount": len(before_after.get("unexpectedChangedSegmentIds") or []),
            "localMutationTouchedRoutePairCount": len(touched_pairs or []),
            "localMutationOrphanVersionCount": len(orphan_versions),
            "localMutationTransactionCount": len(transactions),
        }

    @staticmethod
    def _candidate_selection_metrics(artifact: dict[str, Any]) -> dict[str, Any]:
        snapshot = artifact.get("sessionSnapshot") if isinstance(artifact.get("sessionSnapshot"), dict) else {}
        patches = snapshot.get("itinerary_patches") if isinstance(snapshot.get("itinerary_patches"), list) else []
        turns = snapshot.get("conversation_turns") if isinstance(snapshot.get("conversation_turns"), list) else []
        current_user_turn = next(
            (turn for turn in reversed(turns) if isinstance(turn, dict) and turn.get("role") == "user"),
            {},
        )
        current_turn_id = str(current_user_turn.get("id") or "")
        candidate_patches = []
        for patch in patches:
            if (
                not isinstance(patch, dict)
                or patch.get("validation_status") != "accepted"
                or not current_turn_id
                or str(patch.get("source_turn_id") or "") != current_turn_id
            ):
                continue
            operations = patch.get("operations_json") if isinstance(patch.get("operations_json"), list) else []
            if any(
                isinstance(operation, dict)
                and operation.get("op") == "replace_segment_poi_from_candidate"
                and str(operation.get("candidateId") or "").strip()
                for operation in operations
            ):
                candidate_patches.append(patch)
        changed_segment_ids = {
            str(operation.get("segmentId") or "")
            for patch in candidate_patches
            for operation in (patch.get("operations_json") or [])
            if isinstance(operation, dict) and str(operation.get("segmentId") or "")
        }
        return {
            "candidateSelectionPatchCount": len(candidate_patches),
            "candidateSelectionUsesIdentity": bool(candidate_patches),
            "candidateSelectionChangedSegmentCount": len(changed_segment_ids),
        }

    def _route_pair_metrics(self, snapshot: dict[str, Any], changed_segment_ids: set[str]) -> dict[str, Any]:
        routes = snapshot.get("routeOptions") if isinstance(snapshot.get("routeOptions"), list) else []
        pairs = {
            (str(route.get("fromSegmentId") or ""), str(route.get("toSegmentId") or ""))
            for route in routes
            if isinstance(route, dict) and route.get("fromSegmentId") and route.get("toSegmentId")
        }
        outside = [
            pair
            for pair in pairs
            if changed_segment_ids and pair[0] not in changed_segment_ids and pair[1] not in changed_segment_ids
        ]
        return {
            "routePairCount": len(pairs),
            "routePairsOutsideChangedSegmentCount": len(outside),
        }

    def _route_optimization_metrics(self, artifact: dict[str, Any]) -> dict[str, Any]:
        events = []
        for key in ("planningSteps", "toolEvents"):
            value = artifact.get(key)
            if isinstance(value, list):
                events.extend(item for item in value if isinstance(item, dict))
        previews = [self._event_result_preview(event) for event in events]
        context = artifact.get("context") if isinstance(artifact.get("context"), dict) else {}
        containers: list[dict[str, Any]] = []
        for preview in previews:
            if not isinstance(preview, dict):
                continue
            containers.append(preview)
            route_optimization = preview.get("routeOptimization")
            if isinstance(route_optimization, dict):
                containers.append(route_optimization)
            understood = preview.get("understoodRequirements")
            if isinstance(understood, dict):
                containers.append(understood)
                nested = understood.get("routeOptimization")
                if isinstance(nested, dict):
                    containers.append(nested)
        if isinstance(context.get("understoodRequirements"), dict):
            understood = context["understoodRequirements"]
            containers.append(understood)
            nested = understood.get("routeOptimization")
            if isinstance(nested, dict):
                containers.append(nested)

        objectives: set[str] = set()
        changed_count = 0
        schedule_updated_count = 0
        for item in containers:
            objective = str(item.get("objective") or item.get("optimizationObjective") or "").strip()
            if objective:
                objectives.add(objective)
            changed_count += self._safe_int(item.get("changedCount"))
            schedule_updated_count = max(schedule_updated_count, self._safe_int(item.get("scheduleUpdatedCount")))
        return {
            "routeOptimizationObjectives": sorted(objectives),
            "routeOptimizationChangedCount": changed_count,
            "scheduleUpdatedCount": schedule_updated_count,
        }

    def _tool_budget_metrics(self, artifact: dict[str, Any]) -> dict[str, Any]:
        events = []
        for key in ("planningSteps", "toolEvents"):
            value = artifact.get(key)
            if isinstance(value, list):
                events.extend(item for item in value if isinstance(item, dict))
        previews = [self._event_result_preview(event) for event in events]
        budget_snapshots = [
            preview.get("amapCallBudget") for preview in previews if isinstance(preview.get("amapCallBudget"), dict)
        ]
        tool_round_count = 0
        for event in events:
            preview = self._event_result_preview(event)
            diagnostics = (
                preview.get("toolLoopDiagnostics") if isinstance(preview.get("toolLoopDiagnostics"), dict) else {}
            )
            try:
                tool_round_count = max(tool_round_count, int(diagnostics.get("roundCount") or 0))
            except (TypeError, ValueError):
                pass
            if event.get("type") == "tool_loop_round":
                tool_round_count += 1
        return {
            "toolRoundsUsed": tool_round_count,
            "webSearchCallCount": self._event_tool_count(events, previews, "web_search", "webSearchCalls"),
            "ticketLookupCallCount": self._event_tool_count(events, previews, "ticket_lookup", "ticketLookupCalls"),
            "amapWeatherCallCount": self._event_tool_count(events, previews, "amap_weather", "amapWeatherCalls"),
            "amapPoiExternalCallCount": self._max_budget_value(budget_snapshots, "usedTotalExternal"),
            "amapPoiTextExternalCallCount": self._max_budget_value(budget_snapshots, "usedPlaceText"),
            "amapPoiAroundExternalCallCount": self._max_budget_value(budget_snapshots, "usedPlaceAround"),
            "amapRouteExternalCallCount": self._max_budget_value(budget_snapshots, "usedRoute"),
            "amapCacheHitCount": self._max_budget_value(budget_snapshots, "cacheHitCount"),
            "amapSkippedBecauseBudget": self._max_budget_value(budget_snapshots, "skippedBecauseBudget"),
            "patchOperationCount": max(
                (self._safe_int(preview.get("operationCount")) for preview in previews), default=0
            ),
        }

    def _latency_duration_budget_metrics(self, artifact: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        events = [
            item
            for key in ("planningSteps", "toolEvents")
            for item in (artifact.get(key) or [])
            if isinstance(item, dict)
        ]
        previews = [self._event_result_preview(event) for event in events]
        timing = next(
            (preview.get("timing") for preview in reversed(previews) if isinstance(preview.get("timing"), dict)),
            {},
        )
        segments = self._segments(snapshot.get("days") if isinstance(snapshot.get("days"), list) else [])
        duration_metadata_count = 0
        user_locked_count = 0
        for segment in segments:
            metadata = segment.get("estimateMetadata") if isinstance(segment.get("estimateMetadata"), dict) else {}
            duration = metadata.get("duration") if isinstance(metadata.get("duration"), dict) else {}
            if all(
                duration.get(key) is not None
                for key in ("minMinutes", "preferredMinutes", "maxMinutes", "source", "confidence", "userLocked")
            ):
                duration_metadata_count += 1
            if duration.get("userLocked") is True:
                user_locked_count += 1
        route_status = next(
            (str(preview.get("routeStatus")) for preview in reversed(previews) if preview.get("routeStatus")), ""
        )
        consistency = self._route_schedule_campus_budget_metrics(snapshot)
        event_route_ready = route_status in {"route_ready", "not_required"}
        snapshot_route_ready = (
            int(consistency.get("requiredRouteLegCount") or 0) > 0
            and int(consistency.get("missingRouteLegCount") or 0) == 0
            and int(consistency.get("scheduleConflictCount") or 0) == 0
        )
        return {
            "budgetTier": str(snapshot.get("budgetTier") or "unknown"),
            "durationMetadataSegmentCount": duration_metadata_count,
            "durationMetadataComplete": bool(segments) and duration_metadata_count == len(segments),
            "userLockedDurationCount": user_locked_count,
            "coreDraftDurationMs": self._safe_int(timing.get("coreDraftDurationMs")),
            "routeReadyDurationMs": timing.get("routeReadyDurationMs"),
            "optionalEnrichmentDurationMs": self._safe_int(timing.get("optionalEnrichmentDurationMs")),
            "totalRunDurationMs": self._safe_int(timing.get("totalRunDurationMs")),
            "deadlineBudgetMs": self._safe_int(timing.get("deadlineBudgetMs")),
            "deadlineExceeded": bool(timing.get("deadlineExceeded")),
            "routeReady": event_route_ready and snapshot_route_ready,
            "routeStatus": route_status,
            "eventRouteReady": event_route_ready,
            "snapshotRouteReady": snapshot_route_ready,
            "routeStatusEventSnapshotMismatch": event_route_ready != snapshot_route_ready,
            **consistency,
        }

    def _route_schedule_campus_budget_metrics(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        days = snapshot.get("days") if isinstance(snapshot.get("days"), list) else []
        segments = self._segments(days)
        routes = snapshot.get("routeOptions") if isinstance(snapshot.get("routeOptions"), list) else []
        campus_segments = []
        for segment in segments:
            poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
            text = " ".join(
                str(value or "")
                for value in (poi.get("name"), poi.get("type"), poi.get("category"), segment.get("notes"))
            )
            if (
                segment.get("kind") in {"visit", "activity"}
                and str(poi.get("source") or "") == "amap-place-search"
                and re.search(r"(大学|高校|校园|campus)", text, re.IGNORECASE)
            ):
                campus_segments.append(segment)
        non985 = []
        for segment in campus_segments:
            poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
            candidate = type("CampusMetricCandidate", (), poi)()
            if not self.campus_candidate_policy.matches_tier(candidate, "985"):
                non985.append(str(poi.get("name") or ""))

        required_pairs: list[tuple[str, str]] = []
        for day in days:
            anchors = [segment for segment in day.get("segments") or [] if self._metric_route_anchor(segment)]
            required_pairs.extend((str(a.get("id")), str(b.get("id"))) for a, b in zip(anchors, anchors[1:]))
        usable_routes = [route for route in routes if not route.get("error")]
        covered_pairs = {
            (str(route.get("fromSegmentId") or ""), str(route.get("toSegmentId") or "")) for route in usable_routes
        }
        selected_by_pair = {
            (str(route.get("fromSegmentId") or ""), str(route.get("toSegmentId") or "")): route
            for route in usable_routes
            if route.get("isSelected")
        }
        segment_by_id = {str(segment.get("id")): segment for segment in segments}
        conflicts = 0
        for pair in required_pairs:
            route = selected_by_pair.get(pair)
            if not route:
                continue
            current = segment_by_id.get(pair[0]) or {}
            nxt = segment_by_id.get(pair[1]) or {}
            current_end = self._clock_minutes(str(current.get("endTime") or ""))
            next_start = self._clock_minutes(str(nxt.get("startTime") or ""))
            duration = self._safe_int(route.get("durationSeconds"))
            if (
                current_end is not None
                and next_start is not None
                and next_start < current_end + (duration + 59) // 60 + 10
            ):
                conflicts += 1
        initial_alternatives = sum(
            1
            for route in usable_routes
            if str(route.get("mode") or route.get("transportMode") or "") not in {"transit", "public_transit"}
        )
        cost_ranges = []
        for segment in segments:
            metadata = segment.get("estimateMetadata") if isinstance(segment.get("estimateMetadata"), dict) else {}
            cost = metadata.get("cost") if isinstance(metadata.get("cost"), dict) else {}
            if cost:
                cost_ranges.append(cost)
        hard_constraints = snapshot.get("hardConstraints") if isinstance(snapshot.get("hardConstraints"), dict) else {}
        campus_constraint = (
            hard_constraints.get("campusTier") if isinstance(hard_constraints.get("campusTier"), dict) else {}
        )
        expected_tier = str(campus_constraint.get("value") or "").strip() or None
        expected_count = int(campus_constraint.get("expectedCount") or 0)
        family_mismatches = sum(
            1
            for segment in segments
            if "mealFamilyMismatch=true" in str(segment.get("notes") or "")
            and not re.search(r"diversityRelaxationReason=(?!none|unknown|$)[^;；]+", str(segment.get("notes") or ""))
        )
        unique_coordinates = {
            (
                round(float((segment.get("poi") or {}).get("longitude")), 6),
                round(float((segment.get("poi") or {}).get("latitude")), 6),
            )
            for segment in segments
            if (segment.get("poi") or {}).get("longitude") is not None
            and (segment.get("poi") or {}).get("latitude") is not None
        }
        route_fetch_keys = set()
        for pair in required_pairs:
            current = segment_by_id.get(pair[0]) or {}
            nxt = segment_by_id.get(pair[1]) or {}
            current_poi = current.get("poi") if isinstance(current.get("poi"), dict) else {}
            next_poi = nxt.get("poi") if isinstance(nxt.get("poi"), dict) else {}
            route = selected_by_pair.get(pair) or {}
            if all(
                value is not None
                for value in (
                    current_poi.get("longitude"),
                    current_poi.get("latitude"),
                    next_poi.get("longitude"),
                    next_poi.get("latitude"),
                )
            ):
                route_fetch_keys.add(
                    (
                        round(float(current_poi["longitude"]), 6),
                        round(float(current_poi["latitude"]), 6),
                        round(float(next_poi["longitude"]), 6),
                        round(float(next_poi["latitude"]), 6),
                        str(route.get("mode") or route.get("transportMode") or ""),
                    )
                )
        return {
            "campusTier": expected_tier,
            "campusConstraintSource": campus_constraint.get("source"),
            "expectedCampusCount": expected_count or None,
            "selectedCampusCount": len(campus_segments),
            "non985CampusCount": len(non985),
            "non985CampusNames": non985,
            "hardConstraintRelaxed": campus_constraint.get("relaxed") if campus_constraint else None,
            "requiredRouteLegCount": len(required_pairs),
            "coveredRouteLegCount": sum(1 for pair in required_pairs if pair in covered_pairs),
            "missingRouteLegCount": sum(1 for pair in required_pairs if pair not in covered_pairs),
            "initialAlternativeRouteCallCount": initial_alternatives,
            "scheduleConflictCount": conflicts,
            "familyMismatchSlotCount": family_mismatches,
            "uniquePoiCoordinateCount": len(unique_coordinates),
            "uniqueRouteFetchKeyCount": len(route_fetch_keys),
            "routeFetchDedupedPairCount": max(0, len(required_pairs) - len(route_fetch_keys)),
            "coffeeUsedAsDinnerCount": sum(
                1
                for segment in segments
                if segment.get("kind") == "meal"
                and re.search(r"(晚餐|晚饭|dinner)", str(segment.get("notes") or ""), re.IGNORECASE)
                and re.search(r"(咖啡|coffee|cafe)", str(((segment.get("poi") or {}).get("name")) or ""), re.IGNORECASE)
            ),
            "fullDinnerCoverage": not any(
                segment.get("kind") == "meal"
                and re.search(r"(晚餐|晚饭|dinner)", str(segment.get("notes") or ""), re.IGNORECASE)
                and not self._metric_route_anchor(segment)
                for segment in segments
            ),
            "budgetTarget": snapshot.get("budgetTarget"),
            "budgetEstimateMin": round(sum(float(cost.get("min") or 0) for cost in cost_ranges), 2),
            "budgetEstimatePreferred": round(sum(float(cost.get("preferred") or 0) for cost in cost_ranges), 2),
            "budgetEstimateMax": round(sum(float(cost.get("max") or 0) for cost in cost_ranges), 2),
        }

    def _metric_route_anchor(self, segment: dict[str, Any]) -> bool:
        kind = str(segment.get("kind") or "")
        if kind in {"visit", "activity"}:
            return True
        if kind != "meal":
            return False
        text = str(segment.get("notes") or "") + " " + str(((segment.get("poi") or {}).get("sourceNote")) or "")
        return "routeAnchor=false" not in text and "waiting_for_poi_grounding" not in text

    @staticmethod
    def _clock_minutes(value: str) -> Optional[int]:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
        return int(match.group(1)) * 60 + int(match.group(2)) if match else None

    def _event_tool_count(
        self, events: list[dict[str, Any]], previews: list[dict[str, Any]], tool_name: str, preview_key: str
    ) -> int:
        event_count = sum(1 for event in events if str(event.get("toolName") or event.get("label") or "") == tool_name)
        preview_count = max((self._safe_int(preview.get(preview_key)) for preview in previews), default=0)
        return max(event_count, preview_count)

    def _max_budget_value(self, snapshots: list[dict[str, Any]], key: str) -> int:
        values: list[int] = []
        for snapshot in snapshots:
            raw_value = snapshot.get(key)
            if raw_value is None and isinstance(snapshot.get("used"), dict):
                raw_value = snapshot["used"].get(key)
            values.append(self._safe_int(raw_value))
        return max(values) if values else 0

    def _safe_int(self, value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _combined_artifact_text(self, artifact: dict[str, Any], final_response: dict[str, Any]) -> str:
        parts: list[str] = [
            str(final_response.get("assistantReply") or ""),
            json.dumps(final_response.get("warnings") or [], ensure_ascii=False, default=str),
        ]
        for key in ("planningSteps", "toolEvents"):
            for item in artifact.get(key) or []:
                if isinstance(item, dict):
                    parts.append(json.dumps(item, ensure_ascii=False, default=str))
        return "\n".join(part for part in parts if part)

    def _meal_cost_metrics(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        mismatch_count = 0
        checked_count = 0
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict) or segment.get("kind") != "meal":
                    continue
                estimated_cost = float(segment.get("estimatedCost") or 0)
                if estimated_cost <= 0:
                    continue
                checked_count += 1
                notes = " ".join(
                    [
                        str(segment.get("notes") or ""),
                        str((segment.get("poi") or {}).get("sourceNote") or ""),
                    ]
                )
                per_person = self._number_marker(notes, "costPerPerson")
                party_size = self._number_marker(notes, "partySize")
                total = self._number_marker(notes, "totalCost")
                if "costBasis=per_person" not in notes or per_person <= 0 or party_size <= 0 or total <= 0:
                    mismatch_count += 1
                    continue
                if abs((per_person * party_size) - total) > 0.01 or abs(total - estimated_cost) > 0.01:
                    mismatch_count += 1
        return {
            "mealCostPerPersonCheckedCount": checked_count,
            "mealCostPerPersonMismatchCount": mismatch_count,
        }

    def _event_result_preview(self, event: dict[str, Any]) -> dict[str, Any]:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        preview = metadata.get("resultPreview") if isinstance(metadata.get("resultPreview"), dict) else {}
        return preview

    def _day_by_number(self, snapshot: dict[str, Any], day_number: int) -> dict[str, Any]:
        for day in snapshot.get("days") or []:
            if isinstance(day, dict) and int(day.get("dayNumber") or 0) == day_number:
                return day
        return {}

    def _segment_start_minutes(self, segment: dict[str, Any]) -> int:
        value = str(segment.get("startTime") or "00:00")
        try:
            hours, minutes = value.split(":", 1)
            return int(hours) * 60 + int(minutes)
        except ValueError:
            return 0

    def _number_marker(self, text: str, key: str) -> float:
        match = re.search(rf"{re.escape(key)}=([0-9]+(?:\.[0-9]+)?)", text)
        return float(match.group(1)) if match else 0.0

    def _fallback_confirmation_metrics(self, session_snapshot: dict[str, Any]) -> dict[str, int]:
        executions = session_snapshot.get("agent_choice_executions")
        if not isinstance(executions, list):
            executions = session_snapshot.get("choice_executions")
        executions = executions if isinstance(executions, list) else []
        turns = session_snapshot.get("conversation_turns")
        turns = turns if isinstance(turns, list) else []
        assistant_choice_id_sets: list[tuple[str, ...]] = []
        execution_start_counts: dict[tuple[str, int], int] = {}
        false_write_events = 0
        synthetic_preference_events = 0
        deterministic_safe_draft_events = 0
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            request = turn.get("agent_request_json") if isinstance(turn.get("agent_request_json"), dict) else {}
            response = turn.get("agent_response_json") if isinstance(turn.get("agent_response_json"), dict) else {}
            options = response.get("choiceOptions") if isinstance(response.get("choiceOptions"), list) else []
            option_ids = tuple(
                str(option.get("id") or "") for option in options if isinstance(option, dict) and option.get("id")
            )
            if option_ids:
                assistant_choice_id_sets.append(option_ids)
            events = response.get("planningSteps") if isinstance(response.get("planningSteps"), list) else []
            if response.get("mode") == "clarification":
                false_write_events += sum(
                    1
                    for event in events
                    if isinstance(event, dict) and event.get("type") in {"apply_patch", "create_itinerary_version"}
                )
            selected = (
                request.get("selectedAgentChoice") if isinstance(request.get("selectedAgentChoice"), dict) else {}
            )
            if request.get("syntheticAgentChoice") and selected.get("action") != "manual_continuation":
                synthetic_preference_events += sum(
                    1
                    for event in events
                    if isinstance(event, dict)
                    and str(event.get("type") or "") in {"preference_extract", "preference_memory_write"}
                )
            deterministic_safe_draft_events += sum(
                1
                for event in events
                if isinstance(event, dict)
                and event.get("type") == "initial_day_slot_provider"
                and self._event_result_preview(event).get("executionMode") == "user_confirmed_safe_fallback"
            )
            for event in events:
                if not isinstance(event, dict) or event.get("type") != "fallback_execution_started":
                    continue
                preview = self._event_result_preview(event)
                execution_key = (str(preview.get("executionId") or ""), int(preview.get("attempt") or 0))
                if execution_key[0]:
                    execution_start_counts[execution_key] = execution_start_counts.get(execution_key, 0) + 1
        versions = session_snapshot.get("itinerary_versions")
        versions = versions if isinstance(versions, list) else []
        choice_result_version_ids = {
            str(item.get("result_version_id") or item.get("resultVersionId"))
            for item in executions
            if isinstance(item, dict)
            and item.get("status") == "succeeded"
            and (item.get("result_version_id") or item.get("resultVersionId"))
        }
        created_version_ids = {
            str(item.get("id"))
            for item in versions
            if isinstance(item, dict) and str(item.get("id") or "") in choice_result_version_ids
        }
        return {
            "ruleSafeDraftConfirmedCount": sum(
                1
                for item in executions
                if isinstance(item, dict)
                and item.get("action") == "confirm_rule_safe_draft"
                and item.get("status") == "succeeded"
            ),
            "deterministicSafeDraftExecutionCount": deterministic_safe_draft_events,
            "controllerRetryCount": sum(
                1
                for item in executions
                if isinstance(item, dict)
                and item.get("action") == "retry_model_planning"
                and item.get("status") == "succeeded"
            ),
            "choiceConsumedCount": sum(
                1 for item in executions if isinstance(item, dict) and item.get("status") == "succeeded"
            ),
            "choiceExpiredCount": sum(
                1 for item in executions if isinstance(item, dict) and item.get("status") == "expired"
            ),
            "duplicateChoiceExecutionCount": sum(max(0, count - 1) for count in execution_start_counts.values()),
            "fallbackChoiceVersionDelta": len(created_version_ids),
            "syntheticChoicePreferenceExtractionCount": synthetic_preference_events,
            "falseTimelineWriteEventCount": false_write_events,
            "repeatedSameClarificationCount": sum(
                1
                for previous, current in zip(assistant_choice_id_sets, assistant_choice_id_sets[1:])
                if previous == current
            ),
        }

    def _multi_turn_metrics(self, turn_results: list[dict[str, Any]]) -> dict[str, Any]:
        last = turn_results[-1] if turn_results else {}
        metrics = dict(last.get("metrics") or {})
        original_message = str(turn_results[0].get("input") or "") if turn_results else ""
        final_response = last.get("finalResponse") if isinstance(last.get("finalResponse"), dict) else {}
        version_ids = [str((item.get("metrics") or {}).get("activeVersionId") or "") for item in turn_results]
        metrics.update(
            {
                "turnCount": len(turn_results),
                "activeVersionEventuallyCreated": any(
                    (item.get("metrics") or {}).get("activeVersionCreated") for item in turn_results
                ),
                "secondTurnDoesNotAskForTripDates": not self._asks_for_known_trip_basics(
                    str(final_response.get("assistantReply") or "")
                ),
                "effectiveUserMessageContainsOriginalTrip": self._effective_message_contains_original_trip(
                    str(metrics.get("effectiveUserMessage") or ""),
                    original_message,
                ),
                "noRawProviderRateLimitAsOnlyReply": str(final_response.get("assistantReply") or "").strip()
                not in {"地图服务限流，稍后重试。", "provider_rate_limited"},
                "initialPlanReused": any(self._turn_reused_initial_plan(item) for item in turn_results[1:]),
                "rateLimitDoesNotCreateUnassignedPoolNoise": sum(
                    int((item.get("metrics") or {}).get("unassignedPoolNoiseCount") or 0) for item in turn_results
                )
                == 0,
                "planningPreviewPresent": any(
                    (item.get("metrics") or {}).get("planningPreviewPresent") for item in turn_results
                ),
                "turnRuntimeStatuses": [str(item.get("runtimeStatus") or "") for item in turn_results],
                "activeVersionIdsByTurn": version_ids,
                "activeVersionChangeCount": sum(
                    1
                    for previous, current in zip(version_ids, version_ids[1:])
                    if previous and current and previous != current
                ),
                "repeatedAmapQueryCount": sum(
                    int((item.get("metrics") or {}).get("repeatedAmapQueryCount") or 0) for item in turn_results
                ),
                "duplicateExternalQueryCount": sum(
                    int((item.get("metrics") or {}).get("duplicateExternalQueryCount") or 0) for item in turn_results
                ),
                "controllerDecisionSuccessRate": self._ratio(
                    [item.get("metrics") or {} for item in turn_results],
                    lambda item: (
                        item.get("agentDecisionSource")
                        in {"controller", "deterministic_arbitrator", "deterministic_fast_path"}
                    ),
                ),
                "naturalLanguageTurnCount": sum(
                    int((item.get("metrics") or {}).get("naturalLanguageTurnCount") or 0) for item in turn_results
                ),
                "modelPrimaryDecisionCount": sum(
                    int((item.get("metrics") or {}).get("modelPrimaryDecisionCount") or 0) for item in turn_results
                ),
                "modelPrimaryDecisionRatio": self._ratio(
                    [
                        item.get("metrics") or {}
                        for item in turn_results
                        if int((item.get("metrics") or {}).get("naturalLanguageTurnCount") or 0) > 0
                    ],
                    lambda item: int(item.get("modelPrimaryDecisionCount") or 0) > 0,
                ),
                **{
                    key: sum(int((item.get("metrics") or {}).get(key) or 0) for item in turn_results)
                    for key in (
                        "deterministicBusinessPreemptionCount",
                        "plannerCalledOnHealthyControllerCount",
                        "timelineParserPreDecisionCount",
                        "legacyPhaseRouterPreemptionCount",
                        "deterministicInitialPlanUsedCount",
                        "controllerFallbackCount",
                        "controllerFallbackWriteCount",
                        "executionRouteMismatchCount",
                        "genericToolLoopWithoutAcceptedDecisionCount",
                        "actionDirectiveModelCount",
                        "postObservationDecisionCount",
                        "verifiedWriteCount",
                        "rollbackCount",
                        "manualChoiceRequiredCount",
                        "unexecutedAcceptedContinuingDecisionCount",
                        "falseSuccessClaimCount",
                        "transactionEvidenceMissingCount",
                        "durationPreservationFailureCount",
                    )
                },
                "controllerFallbackRate": self._ratio(
                    [item.get("metrics") or {} for item in turn_results],
                    lambda item: item.get("agentDecisionSource") == "planner_fallback",
                ),
                "controllerTimeoutRate": self._ratio(
                    [item.get("metrics") or {} for item in turn_results],
                    lambda item: "TimeoutError" in str(item.get("controllerError") or ""),
                ),
                "decisionExecutionMismatchRate": self._ratio(
                    [item.get("metrics") or {} for item in turn_results],
                    lambda item: (
                        bool(item.get("actualExecutionRoute"))
                        and str(item.get("agentDecisionProposedExecutionRoute") or "")
                        != str(item.get("actualExecutionRoute") or "")
                    ),
                ),
                "noProgressStopCount": sum(
                    1
                    for item in turn_results
                    if (item.get("metrics") or {}).get("autonomyStopReason") == "repeated_state_fingerprint"
                ),
                "meanCyclesPerRun": (
                    sum(int((item.get("metrics") or {}).get("autonomyObservationCount") or 0) for item in turn_results)
                    / len(turn_results)
                    if turn_results
                    else 0.0
                ),
                "maxCyclesObserved": max(
                    (int((item.get("metrics") or {}).get("autonomyObservationCount") or 0) for item in turn_results),
                    default=0,
                ),
                "verifiedWriteSuccessRate": self._ratio(
                    [
                        item.get("metrics") or {}
                        for item in turn_results
                        if (item.get("metrics") or {}).get("activeVersionCreated")
                    ],
                    lambda item: item.get("outcomeVerifierPassed") is True,
                ),
                "rollbackRate": self._ratio(
                    [item.get("metrics") or {} for item in turn_results],
                    lambda item: bool(item.get("rollbackPerformed")),
                ),
            }
        )
        return metrics

    @staticmethod
    def _ratio(items: list[dict[str, Any]], predicate) -> float:
        return sum(1 for item in items if predicate(item)) / len(items) if items else 0.0

    def _expectation_failures(self, metrics: dict[str, Any], expectations: dict[str, Any]) -> list[str]:
        failures: list[str] = []
        if expectations.get("agentDecisionEventRequired") and not metrics.get("agentDecisionEventPresent"):
            failures.append("agent_decision planning event is missing")
        for expectation_key, metric_key in (
            ("agentDecisionPrimaryAction", "agentDecisionPrimaryAction"),
            ("agentDecisionEffectiveWriteRisk", "agentDecisionEffectiveWriteRisk"),
            ("agentDecisionStopConditionType", "agentDecisionStopConditionType"),
            ("agentDecisionSource", "agentDecisionSource"),
            ("agentDecisionProposedExecutionRoute", "agentDecisionProposedExecutionRoute"),
            ("agentDecisionPolicyAccepted", "agentDecisionPolicyAccepted"),
            ("actualExecutionRoute", "actualExecutionRoute"),
        ):
            if expectation_key in expectations and metrics.get(metric_key) != expectations[expectation_key]:
                failures.append(
                    f"{metric_key} mismatch: expected {expectations[expectation_key]}, got {metrics.get(metric_key)}"
                )
        for expectation_key, metric_key in (
            ("agentDecisionRequiredToolsExact", "agentDecisionRequiredTools"),
            ("agentDecisionEffectiveToolsExact", "agentDecisionEffectiveTools"),
        ):
            if expectation_key in expectations and sorted(metrics.get(metric_key) or []) != sorted(
                expectations[expectation_key] or []
            ):
                failures.append(
                    f"{metric_key} mismatch: expected {expectations[expectation_key]}, got {metrics.get(metric_key)}"
                )
        max_decision_ms = expectations.get("maxAgentDecisionDurationMs")
        if max_decision_ms is not None and int(metrics.get("agentDecisionDurationMs") or 0) > int(max_decision_ms):
            failures.append(
                f"agent decision duration exceeded: expected <= {int(max_decision_ms)}ms, got {metrics.get('agentDecisionDurationMs')}ms"
            )
        if expectations.get("mustCreateTimeline") and not metrics["activeVersionCreated"]:
            failures.append("expected active itinerary version with timeline")
        expected_option_count = expectations.get("localPoiOptionCount")
        if expected_option_count is not None and int(metrics.get("localPoiOptionCount") or 0) != int(
            expected_option_count
        ):
            failures.append(
                f"local POI option count mismatch: expected {int(expected_option_count)}, got {metrics.get('localPoiOptionCount')}"
            )
        if expectations.get("localPoiManualOptionMustBeAvailable") and not metrics.get("localPoiManualOptionAvailable"):
            failures.append("manual local POI option is missing")
        expected_version_changes = expectations.get("activeVersionChangeCount")
        if expected_version_changes is not None and int(metrics.get("activeVersionChangeCount") or 0) != int(
            expected_version_changes
        ):
            failures.append(
                f"active version change count mismatch: expected {int(expected_version_changes)}, got {metrics.get('activeVersionChangeCount')}"
            )
        expected_turn_count = expectations.get("turnCount")
        if expected_turn_count is not None and int(metrics.get("turnCount") or 0) != int(expected_turn_count):
            failures.append(f"turn count mismatch: expected {int(expected_turn_count)}, got {metrics.get('turnCount')}")
        min_days = int(expectations.get("minDays") or 0)
        if min_days and metrics["dayCount"] < min_days:
            failures.append(f"expected at least {min_days} days, got {metrics['dayCount']}")
        expected_day_count = expectations.get("dayCount")
        if expected_day_count is not None and int(metrics.get("dayCount") or 0) != int(expected_day_count):
            failures.append(f"day count mismatch: expected {int(expected_day_count)}, got {metrics.get('dayCount')}")
        min_anchors = int(expectations.get("minRouteableAnchorsPerDay") or 0)
        if min_anchors:
            for day_number, count in metrics["routeableAnchorsByDay"].items():
                if int(count) < min_anchors:
                    failures.append(f"day {day_number} expected at least {min_anchors} routeable anchors, got {count}")
        if expectations.get("allowOrdinaryMealPlaceholder") is False and metrics["ordinaryMealPlaceholders"]:
            failures.append(f"ordinary meal placeholders are not allowed, got {metrics['ordinaryMealPlaceholders']}")
        if expectations.get("ordinaryMealMustBeExpandable") and not metrics["ordinaryMealExpandable"]:
            failures.append("ordinary meal placeholder is not expandable through meal candidate flow")
        if expectations.get("explicitMealMustBeGroundedOrPending") and not metrics["explicitMealGrounded"]:
            failures.append("explicit food experience meal is neither grounded nor pending")
        if expectations.get("explicitLocalFoodMustBeRelevant") and metrics.get("explicitLocalFoodGenericCount"):
            failures.append(
                f"explicit local food selected generic restaurants: {metrics['explicitLocalFoodGenericCount']}"
            )
        if expectations.get("forbidMockOrSyntheticMealsMapReady") and int(
            metrics.get("mockOrSyntheticMealMapReadyCount") or 0
        ):
            failures.append(f"mock/synthetic meals marked map-ready: {metrics['mockOrSyntheticMealMapReadyCount']}")
        if expectations.get("forbidMockOrSyntheticMealsRouteable") and int(
            metrics.get("mockOrSyntheticMealRouteableCount") or 0
        ):
            failures.append(f"mock/synthetic meals marked routeable: {metrics['mockOrSyntheticMealRouteableCount']}")
        if expectations.get("forbidInstitutionalOrHotelMealsForLocalFood"):
            institutional_or_hotel = int(metrics.get("institutionalMealCount") or 0) + int(
                metrics.get("hotelMealCount") or 0
            )
            if institutional_or_hotel:
                failures.append(f"institutional/hotel meals selected for local food: {institutional_or_hotel}")
        if expectations.get("forbidPoorRouteQuality") and metrics.get("routeQualityStatus") == "poor":
            failures.append("route quality is poor")
        if expectations.get("forbidMealDetourHigh") and int(metrics.get("mealDetourHighCount") or 0):
            failures.append(f"meal detour high/poor routes found: {metrics['mealDetourHighCount']}")
        max_day_km = float(expectations.get("maxTotalRouteKmPerDay") or 0)
        if max_day_km and float(metrics.get("maxTotalRouteKmPerDay") or 0) > max_day_km:
            failures.append(f"daily route distance exceeds {max_day_km} km: {metrics['maxTotalRouteKmPerDay']}")
        max_meal_leg_km = float(expectations.get("maxMealAdjacentLegKm") or 0)
        if max_meal_leg_km and float(metrics.get("maxMealAdjacentLegKm") or 0) > max_meal_leg_km:
            failures.append(f"meal-adjacent route exceeds {max_meal_leg_km} km: {metrics['maxMealAdjacentLegKm']}")
        preferred_mode = expectations.get("preferredTransportModeMustBeRespected") or expectations.get(
            "preferredTransportMode"
        )
        if preferred_mode and int(metrics.get("nonPreferredRouteModeCount") or 0):
            failures.append(
                f"selected routes violate preferred mode {preferred_mode}: {metrics['nonPreferredRouteModeCount']}"
            )
        max_non_preferred_ratio = expectations.get("maxNonPreferredRouteLegRatio")
        if max_non_preferred_ratio is not None and float(metrics.get("nonPreferredRouteLegRatio") or 0) > float(
            max_non_preferred_ratio
        ):
            failures.append(
                f"non-preferred route ratio exceeds {float(max_non_preferred_ratio):.2f}: {metrics['nonPreferredRouteLegRatio']}"
            )
        if expectations.get("nonPreferredRouteRequiresCaveat") and int(
            metrics.get("nonPreferredRouteWithoutCaveatCount") or 0
        ):
            failures.append(
                f"non-preferred route modes missing caveat: {metrics['nonPreferredRouteWithoutCaveatCount']}"
            )
        required_route_modes = expectations.get("selectedRouteModesMustInclude")
        if isinstance(required_route_modes, list):
            selected_modes = {str(item) for item in metrics.get("selectedRouteModes") or []}
            missing_modes = [str(item) for item in required_route_modes if str(item) not in selected_modes]
            if missing_modes:
                failures.append(f"selected route modes missing: {missing_modes}")
        min_selected_routes = expectations.get("minSelectedRouteCount")
        if min_selected_routes is not None and int(metrics.get("selectedRouteCount") or 0) < int(min_selected_routes):
            failures.append(
                f"selected route count below {int(min_selected_routes)}: {metrics.get('selectedRouteCount')}"
            )
        expected_route_pairs = expectations.get("routePairCount")
        if expected_route_pairs is not None and int(metrics.get("routePairCount") or 0) != int(expected_route_pairs):
            failures.append(
                f"route pair count mismatch: expected {int(expected_route_pairs)}, got {metrics.get('routePairCount')}"
            )
        if expectations.get("routePairsMustTouchChangedSegments") and int(
            metrics.get("routePairsOutsideChangedSegmentCount") or 0
        ):
            failures.append(
                f"route pairs outside changed segments: {metrics.get('routePairsOutsideChangedSegmentCount')}"
            )
        required_fallback_reasons = expectations.get("nonPreferredRouteFallbackReasonsMustInclude")
        if isinstance(required_fallback_reasons, list):
            fallback_reasons = {str(item) for item in metrics.get("nonPreferredRouteFallbackReasons") or []}
            missing_reasons = [str(item) for item in required_fallback_reasons if str(item) not in fallback_reasons]
            if missing_reasons:
                failures.append(f"non-preferred route fallback reasons missing: {missing_reasons}")
        required_objectives = expectations.get("routeOptimizationObjectivesMustInclude")
        if isinstance(required_objectives, list):
            observed_objectives = {str(item) for item in metrics.get("routeOptimizationObjectives") or []}
            missing_objectives = [str(item) for item in required_objectives if str(item) not in observed_objectives]
            if missing_objectives:
                failures.append(f"route optimization objectives missing: {missing_objectives}")
        min_route_changes = expectations.get("routeOptimizationChangedCountAtLeast")
        if min_route_changes is not None and int(metrics.get("routeOptimizationChangedCount") or 0) < int(
            min_route_changes
        ):
            failures.append(
                f"route optimization changed count below {int(min_route_changes)}: {metrics.get('routeOptimizationChangedCount')}"
            )
        min_schedule_updates = expectations.get("scheduleUpdatedCountAtLeast")
        if min_schedule_updates is not None and int(metrics.get("scheduleUpdatedCount") or 0) < int(
            min_schedule_updates
        ):
            failures.append(
                f"schedule updated count below {int(min_schedule_updates)}: {metrics.get('scheduleUpdatedCount')}"
            )
        expected_segment_times = expectations.get("segmentStartTimes")
        if isinstance(expected_segment_times, dict):
            actual_segment_times = (
                metrics.get("segmentStartTimes") if isinstance(metrics.get("segmentStartTimes"), dict) else {}
            )
            for segment_id, expected_time in expected_segment_times.items():
                actual_time = actual_segment_times.get(str(segment_id))
                if actual_time != str(expected_time):
                    failures.append(f"segment {segment_id} expected start {expected_time}, got {actual_time}")
        if expectations.get("riskSearchMustSkipOrdinaryMeals") and int(metrics.get("riskMealSearchCount") or 0):
            failures.append(f"ordinary meal risk searches found: {metrics['riskMealSearchCount']}")
        if expectations.get("riskSearchProviderDiagnosticsMustBePresent") and not metrics.get(
            "webSearchProviderDiagnosticsPresent"
        ):
            failures.append("web search provider diagnostics missing from risk alerts")
        if expectations.get("riskSearchMustAttemptAtLeastOneStableProvider") and not metrics.get(
            "riskSearchHasAtLeastOneConfiguredStableProvider"
        ):
            failures.append("risk search did not attempt any configured stable provider")
        for expectation_key, metric_key, label in (
            ("riskSearchAttemptedProvidersMustInclude", "webSearchAttemptedProviders", "attempted"),
            ("riskSearchSuccessfulProvidersMustInclude", "webSearchSuccessfulProviders", "successful"),
            ("riskSearchFailedProvidersMustInclude", "webSearchFailedProviders", "failed"),
            ("riskSearchSkippedProvidersMustInclude", "webSearchSkippedProviders", "skipped"),
        ):
            required_providers = expectations.get(expectation_key)
            if isinstance(required_providers, list):
                observed_providers = {str(item) for item in metrics.get(metric_key) or []}
                missing_providers = [str(item) for item in required_providers if str(item) not in observed_providers]
                if missing_providers:
                    failures.append(f"risk search {label} providers missing: {missing_providers}")
        if expectations.get("riskSearchUnavailableBecauseAllProvidersFailed") and not metrics.get(
            "riskSearchUnavailableBecauseAllProvidersFailed"
        ):
            failures.append("risk search was expected to be unavailable because all providers failed")
        min_accepted_sources = expectations.get("riskSearchAcceptedSourceCountAtLeast")
        if min_accepted_sources is not None and int(metrics.get("webSearchAcceptedSourceCount") or 0) < int(
            min_accepted_sources
        ):
            failures.append(
                f"risk search accepted source count below {int(min_accepted_sources)}: {metrics.get('webSearchAcceptedSourceCount')}"
            )
        max_query_chars = expectations.get("riskSearchQueryMaxChars")
        if max_query_chars is not None and int(metrics.get("riskSearchQueryTooLongCount") or 0):
            failures.append(
                f"risk search queries exceed {int(max_query_chars)} chars: {metrics.get('riskSearchQueryTooLongCount')}"
            )
        max_duplicate_queries = expectations.get("riskSearchDuplicateQueryMaxCount")
        if max_duplicate_queries is not None and int(metrics.get("riskSearchDuplicateQueryCount") or 0) > int(
            max_duplicate_queries
        ):
            failures.append(
                f"risk search duplicate query count exceeds {int(max_duplicate_queries)}: {metrics.get('riskSearchDuplicateQueryCount')}"
            )
        if expectations.get("forbidWeakNightViewPoi") and metrics["weakNightViewPoiCount"]:
            failures.append(f"weak night-view POIs selected: {metrics['weakNightViewPoiCount']}")
        if expectations.get("nightViewMustBeDistinctFamily") and metrics.get("nightViewDuplicateFamilyCount"):
            failures.append(f"duplicate night-view families selected: {metrics['nightViewDuplicateFamilyCount']}")
        expected_first_night = expectations.get("firstNightPoiContains")
        if expected_first_night and str(expected_first_night) not in str(metrics.get("firstNightPoi") or ""):
            failures.append(
                f"first-night POI mismatch: expected to contain {expected_first_night}, got {metrics.get('firstNightPoi')}"
            )
        if expectations.get("creativeStyleMustBeDeclared") and not metrics.get("creativeStyleDeclared"):
            failures.append("creative style was not declared in assistant reply or planning metadata")
        if expectations.get("creativeVariantMustBePresent") and not metrics.get("creativeVariantPresent"):
            failures.append("creative variant metadata missing")
        max_repeated_poi_names = expectations.get("maxRepeatedPoiNames")
        if max_repeated_poi_names is not None and int(metrics.get("repeatedPoiNameCount") or 0) > int(
            max_repeated_poi_names
        ):
            failures.append(
                f"repeated POI names exceed {int(max_repeated_poi_names)}: {metrics.get('repeatedPoiNameCount')}"
            )
        if expectations.get("riskUnknownMustNotShowLow") and metrics["riskLowWithoutSourceCount"]:
            failures.append(f"low-like risk shown without reliable source: {metrics['riskLowWithoutSourceCount']}")
        if expectations.get("timelinePrecisionEditFillDay2Dinner"):
            if not metrics.get("timelineEditEventPresent"):
                failures.append("timeline edit event missing")
            if metrics.get("timelineEditOperation") != "replace_or_fill":
                failures.append(f"timeline edit operation mismatch: {metrics.get('timelineEditOperation')}")
            if int(metrics.get("targetDayDinnerCount") or 0) < 1:
                failures.append("target day dinner was not filled")
            if metrics.get("timelineEditGlobalReorder"):
                failures.append("timeline edit reported global reorder")
            if not metrics.get("timelineEditUnchangedDaysMatch"):
                failures.append("timeline edit changed a day that should have stayed unchanged")
        if expectations.get("mealCostPerPersonDisplay") and int(metrics.get("mealCostPerPersonMismatchCount") or 0):
            failures.append(f"meal cost per-person markers mismatch: {metrics.get('mealCostPerPersonMismatchCount')}")
        if expectations.get("mustUseDeterministicTimelineCommand") and not metrics.get("deterministicTimelineCommand"):
            failures.append("expected deterministic timeline command path")
        if expectations.get("localMutationDetected") and not metrics.get("localMutationDetected"):
            failures.append("expected local timeline mutation channel")
        expected_mutation_status = expectations.get("timelineMutationStatus")
        if expected_mutation_status and metrics.get("timelineMutationStatus") != expected_mutation_status:
            failures.append(
                f"timeline mutation status mismatch: expected {expected_mutation_status}, got {metrics.get('timelineMutationStatus')}"
            )
        expected_actual_route = expectations.get("actualExecutionRoute")
        if expected_actual_route and metrics.get("actualExecutionRoute") != expected_actual_route:
            failures.append(
                f"actual execution route mismatch: expected {expected_actual_route}, got {metrics.get('actualExecutionRoute')}"
            )
        expected_bound_ids = expectations.get("exactBoundSegmentIds")
        if isinstance(expected_bound_ids, list) and metrics.get("boundSegmentIds") != [
            str(item) for item in expected_bound_ids
        ]:
            failures.append(
                f"bound segment ids mismatch: expected {expected_bound_ids}, got {metrics.get('boundSegmentIds')}"
            )
        if expectations.get("postconditionMustPass") and metrics.get("postconditionPassed") is not True:
            failures.append("timeline mutation postcondition did not pass")
        if expectations.get("structuralVerifierMustPass") and metrics.get("structuralVerifierPassed") is not True:
            failures.append("timeline mutation structural verifier did not pass")
        if expectations.get("mustNotEnterGenericToolLoop") and metrics.get("genericToolLoopEntered"):
            failures.append("timeline mutation entered generic tool loop")
        if expectations.get("mustNotEnterToolLoop") and metrics.get("toolLoopEntered"):
            failures.append("local edit entered generic tool loop")
        if expectations.get("verifierMustPass") and not metrics.get("verifierPassed"):
            failures.append("verifier report did not pass")
        expected_pool_coverage = expectations.get("requiredIntentPoolCoverage")
        if isinstance(expected_pool_coverage, dict):
            actual_coverage = metrics.get("intentPoolCoverage") or {}
            for intent_type, expected_status in expected_pool_coverage.items():
                if actual_coverage.get(intent_type) != expected_status:
                    failures.append(
                        f"required intent pool {intent_type} coverage mismatch: "
                        f"expected {expected_status}, got {actual_coverage.get(intent_type)}"
                    )
        if expectations.get("requiredPoolsMustPrecedeOptional") and not metrics.get("requiredPoolsPrecedeOptional"):
            failures.append("optional creative pool consumed priority before all required pools")
        for key in (
            "museumSemanticMismatchSelectedCount",
            "museumTimelineInvalidClaimCount",
            "readOnlyExternalCallCount",
            "readOnlyPatchCount",
            "readOnlyVersionDelta",
            "genericToolLoopWithoutDecisionCount",
            "goalClaimMismatchCount",
            "foodExperienceMinCount",
            "foodExperiencePreferredCount",
            "foodExperienceMaxCount",
            "requiredMealTargetCount",
            "naturalLanguageTurnCount",
            "modelPrimaryDecisionCount",
            "deterministicBusinessPreemptionCount",
            "plannerCalledOnHealthyControllerCount",
            "timelineParserPreDecisionCount",
            "legacyPhaseRouterPreemptionCount",
            "deterministicInitialPlanUsedCount",
            "controllerFallbackCount",
            "controllerFallbackWriteCount",
            "executionRouteMismatchCount",
            "falseSuccessClaimCount",
            "transactionEvidenceMissingCount",
            "durationPreservationFailureCount",
            "genericToolLoopWithoutAcceptedDecisionCount",
            "actionDirectiveModelCount",
            "postObservationDecisionCount",
            "verifiedWriteCount",
            "rollbackCount",
            "manualChoiceRequiredCount",
            "unexecutedAcceptedContinuingDecisionCount",
            "ruleSafeDraftConfirmedCount",
            "deterministicSafeDraftExecutionCount",
            "controllerRetryCount",
            "choiceConsumedCount",
            "choiceExpiredCount",
            "duplicateChoiceExecutionCount",
            "fallbackChoiceVersionDelta",
            "syntheticChoicePreferenceExtractionCount",
            "falseTimelineWriteEventCount",
            "repeatedSameClarificationCount",
            "controllerFullCallCount",
            "controllerLiteCallCount",
            "controllerFullSuccessCount",
            "controllerLiteSuccessCount",
            "structuredChoiceIdentityMismatchCount",
            "ruleSafeDraftExecutionCount",
            "controllerCallOnRuleSafeConfirmCount",
            "safeFallbackOfferedCount",
            "versionDelta",
            "patchDelta",
            "falseTimelineMutationSuccessCount",
            "localMutationDetectedCount",
            "localMutationBoundCount",
            "localMutationAmbiguousCount",
            "localMutationTargetNotFoundCount",
            "localMutationControllerBypassCount",
            "localMutationGenericToolLoopCount",
            "localMutationUnrelatedExternalCallCount",
            "localMutationPatchCount",
            "localMutationVersionDelta",
            "localMutationPostconditionPassCount",
            "localMutationRollbackCount",
            "localMutationNoOpCount",
            "localMutationMisleadingSuccessCount",
            "localMutationUnexpectedChangedSegmentCount",
            "localMutationTouchedRoutePairCount",
            "localMutationOrphanVersionCount",
            "localMutationTransactionCount",
            "proposalStageEvidenceUsed",
            "goalOccurrencePlanCount",
            "goalOccurrenceExpectedCount",
            "goalOccurrenceActualCount",
            "goalOccurrenceCoverageFailureCount",
            "dailyGoalCoverageFailureCount",
            "distinctAcrossDaysViolationCount",
            "groundedExplicitMealOccurrenceExpectedCount",
            "groundedExplicitMealOccurrenceActualCount",
            "backendRegexOccurrenceDecisionCount",
            "controllerDayStrategyOccurrenceCount",
            "occurrenceEvidenceMissingCount",
            "selectedProposalFinalOccurrenceMismatchCount",
            "selectedProposalFinalIdentityMismatchCount",
            "adjacentSearchCenterCount",
            "genericAddGlobalSearchBeforeAdjacentCount",
            "insertionRouteVerifiedCandidateCount",
            "mutationPreflightWriteAttemptCount",
            "mutationPreflightVersionDelta",
            "mutationPreflightPatchDelta",
            "localAddSelectedCandidateCount",
            "localAddVersionDelta",
            "localAddPatchDelta",
            "localAddRollbackCount",
            "farOnlyCandidateZeroWriteCount",
            "exactEntitySubstitutionCount",
            "proposalPreviewWriteCount",
            "proposalCommitAttemptCount",
            "versionWriteCount",
            "patchWriteCount",
            "pendingSlotPreservedCount",
        ):
            expected = expectations.get(key)
            if expected is not None and int(metrics.get(key) or 0) != int(expected):
                failures.append(f"{key} mismatch: expected {int(expected)}, got {metrics.get(key)}")
        expected_model_ratio = expectations.get("modelPrimaryDecisionRatio")
        if expected_model_ratio is not None and float(metrics.get("modelPrimaryDecisionRatio") or 0.0) != float(
            expected_model_ratio
        ):
            failures.append(
                "modelPrimaryDecisionRatio mismatch: "
                f"expected {float(expected_model_ratio)}, got {metrics.get('modelPrimaryDecisionRatio')}"
            )
        if (
            expectations.get("readOnlyUnchangedVerifierMustPass")
            and metrics.get("readOnlyUnchangedVerifierPassed") is not True
        ):
            failures.append("read-only unchanged-state verifier did not pass")
        expected_fallback_route = expectations.get("controllerFallbackRoute")
        if expected_fallback_route is not None and str(metrics.get("controllerFallbackRoute") or "") != str(
            expected_fallback_route
        ):
            failures.append(
                "controller fallback route mismatch: "
                f"expected {expected_fallback_route}, got {metrics.get('controllerFallbackRoute')}"
            )
        expected_execution_mode = expectations.get("executionMode")
        if expected_execution_mode and metrics.get("executionMode") != expected_execution_mode:
            failures.append(
                f"execution mode mismatch: expected {expected_execution_mode}, got {metrics.get('executionMode')}"
            )
        expected_terminal_status = expectations.get("terminalStatus")
        if expected_terminal_status and metrics.get("terminalStatus") != expected_terminal_status:
            failures.append(
                f"terminal status mismatch: expected {expected_terminal_status}, got {metrics.get('terminalStatus')}"
            )
        min_decisions = expectations.get("minAgentDecisionCount")
        if min_decisions is not None and int(metrics.get("agentDecisionCount") or 0) < int(min_decisions):
            failures.append(f"agent decision count below {int(min_decisions)}: {metrics.get('agentDecisionCount')}")
        min_visible_actions = expectations.get("minVisibleActionCount")
        if min_visible_actions is not None and int(metrics.get("visibleActionCount") or 0) < int(min_visible_actions):
            failures.append(
                f"visible action count below {int(min_visible_actions)}: {metrics.get('visibleActionCount')}"
            )
        max_schema_repairs = expectations.get("maxSchemaRepairAttempts")
        if max_schema_repairs is not None and int(metrics.get("schemaRepairAttempts") or 0) > int(max_schema_repairs):
            failures.append(
                f"schema repair attempts exceed {int(max_schema_repairs)}: {metrics.get('schemaRepairAttempts')}"
            )
        max_invented_amap = expectations.get("maxInventedAmapPoiCount")
        if max_invented_amap is not None and int(metrics.get("inventedAmapPoiCount") or 0) > int(max_invented_amap):
            failures.append(
                f"invented AMap POI count exceeds {int(max_invented_amap)}: {metrics.get('inventedAmapPoiCount')}"
            )
        max_repeated_queries = expectations.get("maxRepeatedAmapQueryCount")
        if max_repeated_queries is not None and int(metrics.get("repeatedAmapQueryCount") or 0) > int(
            max_repeated_queries
        ):
            failures.append(
                f"repeated AMap meal queries exceed {int(max_repeated_queries)}: {metrics.get('repeatedAmapQueryCount')}"
            )
        min_unique_meal_families = expectations.get("minUniqueMealFamilyCount")
        if min_unique_meal_families is not None and int(metrics.get("uniqueMealFamilyCount") or 0) < int(
            min_unique_meal_families
        ):
            failures.append(
                f"unique meal family count below {int(min_unique_meal_families)}: {metrics.get('uniqueMealFamilyCount')}"
            )
        max_zhajiangmian = expectations.get("maxZhajiangmianCount")
        if max_zhajiangmian is not None and int(metrics.get("zhajiangmianCount") or 0) > int(max_zhajiangmian):
            failures.append(f"zhajiangmian count exceeds {int(max_zhajiangmian)}: {metrics.get('zhajiangmianCount')}")
        max_duplicate_meal_brands = expectations.get("maxDuplicateMealBrandCount")
        if max_duplicate_meal_brands is not None and int(metrics.get("duplicateMealBrandCount") or 0) > int(
            max_duplicate_meal_brands
        ):
            failures.append(
                f"duplicate meal brand count exceeds {int(max_duplicate_meal_brands)}: {metrics.get('duplicateMealBrandCount')}"
            )
        max_tool_rounds = expectations.get("maxToolRoundsUsed")
        if max_tool_rounds is not None and int(metrics.get("toolRoundsUsed") or 0) > int(max_tool_rounds):
            failures.append(f"tool rounds exceed {int(max_tool_rounds)}: {metrics.get('toolRoundsUsed')}")
        max_web_search = expectations.get("maxWebSearchCalls")
        if max_web_search is not None and int(metrics.get("webSearchCallCount") or 0) > int(max_web_search):
            failures.append(f"web_search calls exceed {int(max_web_search)}: {metrics.get('webSearchCallCount')}")
        max_ticket_lookup = expectations.get("maxTicketLookupCalls")
        if max_ticket_lookup is not None and int(metrics.get("ticketLookupCallCount") or 0) > int(max_ticket_lookup):
            failures.append(
                f"ticket_lookup calls exceed {int(max_ticket_lookup)}: {metrics.get('ticketLookupCallCount')}"
            )
        max_weather = expectations.get("maxAmapWeatherCalls")
        if max_weather is not None and int(metrics.get("amapWeatherCallCount") or 0) > int(max_weather):
            failures.append(f"amap_weather calls exceed {int(max_weather)}: {metrics.get('amapWeatherCallCount')}")
        expected_budget_tier = expectations.get("budgetTier")
        if expected_budget_tier is not None and str(metrics.get("budgetTier")) != str(expected_budget_tier):
            failures.append(f"budget tier mismatch: {metrics.get('budgetTier')} != {expected_budget_tier}")
        if expectations.get("durationMetadataMustBeComplete") and not metrics.get("durationMetadataComplete"):
            failures.append("duration estimate metadata is incomplete")
        max_core_draft_ms = expectations.get("maxCoreDraftDurationMs")
        if max_core_draft_ms is not None and int(metrics.get("coreDraftDurationMs") or 0) > int(max_core_draft_ms):
            failures.append(f"core draft duration exceeded: {metrics.get('coreDraftDurationMs')} > {max_core_draft_ms}")
        max_route_ready_ms = expectations.get("maxRouteReadyDurationMs")
        if max_route_ready_ms is not None:
            route_ready_ms = metrics.get("routeReadyDurationMs")
            if route_ready_ms is None or int(route_ready_ms) > int(max_route_ready_ms):
                failures.append(f"route-ready duration missing or exceeded: {route_ready_ms} > {max_route_ready_ms}")
        if expectations.get("deadlineMustNotBeExceeded") and metrics.get("deadlineExceeded"):
            failures.append("staged pipeline deadline exceeded")
        for key in (
            "selectedCampusCount",
            "non985CampusCount",
            "requiredRouteLegCount",
            "coveredRouteLegCount",
            "missingRouteLegCount",
            "initialAlternativeRouteCallCount",
            "scheduleConflictCount",
            "familyMismatchSlotCount",
            "coffeeUsedAsDinnerCount",
            "uniquePoiCoordinateCount",
            "uniqueRouteFetchKeyCount",
            "routeFetchDedupedPairCount",
        ):
            expected = expectations.get(key)
            if expected is not None:
                observed = metrics.get(key)
                if observed is None or int(observed) != int(expected):
                    failures.append(f"{key} mismatch: expected {int(expected)}, got {observed}")
        if expectations.get("hardConstraintMustNotRelax"):
            if metrics.get("hardConstraintRelaxed") is None:
                failures.append("hard campus constraint relaxation evidence is missing")
            elif metrics.get("hardConstraintRelaxed"):
                failures.append("hard campus constraint was relaxed")
        if any(
            expectations.get(key) is not None
            for key in ("requiredRouteLegCount", "coveredRouteLegCount", "missingRouteLegCount")
        ):
            if metrics.get("routeStatusEventSnapshotMismatch"):
                failures.append("route-ready event disagrees with snapshot pair coverage or schedule verifier")
        if expectations.get("fullDinnerCoverage") and not metrics.get("fullDinnerCoverage"):
            failures.append("dinner coverage is incomplete")
        max_amap_poi = expectations.get("maxAmapPoiExternalCalls")
        if max_amap_poi is not None and int(metrics.get("amapPoiExternalCallCount") or 0) > int(max_amap_poi):
            failures.append(
                f"AMap POI external calls exceed {int(max_amap_poi)}: {metrics.get('amapPoiExternalCallCount')}"
            )
        min_amap_poi = expectations.get("minAmapPoiExternalCalls")
        if min_amap_poi is not None and int(metrics.get("amapPoiExternalCallCount") or 0) < int(min_amap_poi):
            failures.append(
                f"AMap POI external calls below {int(min_amap_poi)}: {metrics.get('amapPoiExternalCallCount')}"
            )
        max_amap_text = expectations.get("maxAmapPoiTextExternalCalls")
        if max_amap_text is not None and int(metrics.get("amapPoiTextExternalCallCount") or 0) > int(max_amap_text):
            failures.append(
                f"AMap text external calls exceed {int(max_amap_text)}: {metrics.get('amapPoiTextExternalCallCount')}"
            )
        max_amap_around = expectations.get("maxAmapPoiAroundExternalCalls")
        if max_amap_around is not None and int(metrics.get("amapPoiAroundExternalCallCount") or 0) > int(
            max_amap_around
        ):
            failures.append(
                f"AMap around external calls exceed {int(max_amap_around)}: {metrics.get('amapPoiAroundExternalCallCount')}"
            )
        max_amap_route = expectations.get("maxAmapRouteExternalCalls")
        if max_amap_route is not None and int(metrics.get("amapRouteExternalCallCount") or 0) > int(max_amap_route):
            failures.append(
                f"AMap route external calls exceed {int(max_amap_route)}: {metrics.get('amapRouteExternalCallCount')}"
            )
        min_budget_skips = expectations.get("minAmapSkippedBecauseBudget")
        if min_budget_skips is not None and int(metrics.get("amapSkippedBecauseBudget") or 0) < int(min_budget_skips):
            failures.append(
                f"AMap budget skips below {int(min_budget_skips)}: {metrics.get('amapSkippedBecauseBudget')}"
            )
        modified_scope = expectations.get("modifiedScope")
        if modified_scope and metrics.get("timelineEditChangedScope") != modified_scope:
            failures.append(
                f"modified scope mismatch: expected {modified_scope}, got {metrics.get('timelineEditChangedScope')}"
            )
        exact_changed_ids = expectations.get("exactChangedSegmentIds")
        if isinstance(exact_changed_ids, list) and metrics.get("timelineEditChangedSegmentIds") != [
            str(item) for item in exact_changed_ids
        ]:
            failures.append(
                f"changed segment ids mismatch: expected {exact_changed_ids}, got {metrics.get('timelineEditChangedSegmentIds')}"
            )
        changed_segment_count = expectations.get("changedSegmentCount")
        if changed_segment_count is not None and int(metrics.get("timelineEditChangedSegmentCount") or 0) != int(
            changed_segment_count
        ):
            failures.append(
                f"changed segment count mismatch: expected {int(changed_segment_count)}, got {metrics.get('timelineEditChangedSegmentCount')}"
            )
        if expectations.get("changedMealMustBeFood") and metrics.get("timelineEditChangedMealPoiCategory") != "food":
            failures.append(f"changed meal POI is not food: {metrics.get('timelineEditChangedMealPoiCategory')}")
        if expectations.get("candidateSelectionMustUseIdentity") and not metrics.get("candidateSelectionUsesIdentity"):
            failures.append("candidate selection did not use a persisted candidate identity")
        expected_candidate_patch_count = expectations.get("candidateSelectionPatchCount")
        if expected_candidate_patch_count is not None and int(metrics.get("candidateSelectionPatchCount") or 0) != int(
            expected_candidate_patch_count
        ):
            failures.append(
                f"candidate selection patch count mismatch: expected {int(expected_candidate_patch_count)}, got {metrics.get('candidateSelectionPatchCount')}"
            )
        expected_candidate_changed_count = expectations.get("candidateSelectionChangedSegmentCount")
        if expected_candidate_changed_count is not None and int(
            metrics.get("candidateSelectionChangedSegmentCount") or 0
        ) != int(expected_candidate_changed_count):
            failures.append(
                "candidate selection changed segment count mismatch: "
                f"expected {int(expected_candidate_changed_count)}, got {metrics.get('candidateSelectionChangedSegmentCount')}"
            )
        forbidden_meal_text = expectations.get("changedMealMustNotContain")
        if forbidden_meal_text:
            needles = forbidden_meal_text if isinstance(forbidden_meal_text, list) else [forbidden_meal_text]
            meal_name = str(metrics.get("timelineEditChangedMealPoiName") or "")
            if any(str(needle) and str(needle) in meal_name for needle in needles):
                failures.append(f"changed meal POI still contains forbidden text: {meal_name}")
        forbidden_text = expectations.get("mustNotContainFailure")
        if forbidden_text:
            needle = "Agent tool loop exceeded" if forbidden_text is True else str(forbidden_text)
            if needle and needle in str(metrics.get("combinedText") or ""):
                failures.append(f"forbidden failure text present: {needle}")
        if expectations.get("secondTurnDoesNotAskForTripDates") and not metrics.get("secondTurnDoesNotAskForTripDates"):
            failures.append("second turn asked for already-known trip basics")
        if expectations.get("effectiveUserMessageContainsOriginalTrip") and not metrics.get(
            "effectiveUserMessageContainsOriginalTrip"
        ):
            failures.append("effectiveUserMessage did not preserve original trip request")
        if expectations.get("activeVersionEventuallyCreated") and not metrics.get("activeVersionEventuallyCreated"):
            failures.append("expected active version to be created by the end of multi-turn scenario")
        if expectations.get("noRawProviderRateLimitAsOnlyReply") and not metrics.get(
            "noRawProviderRateLimitAsOnlyReply"
        ):
            failures.append("provider rate limit surfaced as the only raw reply")
        if expectations.get("rateLimitDoesNotCreateUnassignedPoolNoise") and not metrics.get(
            "rateLimitDoesNotCreateUnassignedPoolNoise"
        ):
            failures.append("rate-limited pools produced unassigned/no_intent_pool_candidate noise")
        if expectations.get("planningPreviewMustBePresent") and not metrics.get("planningPreviewPresent"):
            failures.append("expected planningPreview in no-version or retry flow")
        if expectations.get("initialPlanMustBeReused") and not metrics.get("initialPlanReused"):
            failures.append("expected retry turn to reuse previous initialPlan")
        if expectations.get("regeneratePlanningRequestEnabled") and not metrics.get("regeneratePlanningRequestEnabled"):
            failures.append("expected short regenerate turn to reuse the latest complete trip request")
        return failures

    def _asks_for_known_trip_basics(self, text: str) -> bool:
        # Mentions such as "日期不在天气窗口内" are status explanations,
        # not clarification questions. Only count explicit requests/questions.
        return bool(
            re.search(
                r"(?:请补充|还需(?:要)?|需要确认|请告诉我|方便提供).{0,16}(?:日期|哪几天|几天|预算|人数|交通偏好)"
                r"|(?:哪几天|玩几天|几个人|同行人数|预算多少|交通.*偏好).{0,8}(?:吗|呢|？|\?)",
                text,
            )
        )

    def _effective_message_contains_original_trip(self, effective_message: str, original_message: str) -> bool:
        if not original_message.strip():
            return True
        required_terms = [term for term in ["国庆", "高校", "夜景", "美食", "公交", "地铁"] if term in original_message]
        if not required_terms:
            return bool(effective_message.strip())
        return all(term in effective_message for term in required_terms)

    def _turn_reused_initial_plan(self, turn_result: dict[str, Any]) -> bool:
        for event in (turn_result.get("planningSteps") or []) + (turn_result.get("toolEvents") or []):
            preview = ((event.get("metadata") or {}).get("resultPreview") or {}) if isinstance(event, dict) else {}
            if preview.get("providerReturnedMode") == "planning_attempt_resume":
                return True
        return False

    def _planning_preview_present(self, artifact: dict[str, Any]) -> bool:
        for event in (artifact.get("planningSteps") or []) + (artifact.get("toolEvents") or []):
            preview = ((event.get("metadata") or {}).get("resultPreview") or {}) if isinstance(event, dict) else {}
            if preview.get("planningPreview"):
                return True
        return bool((artifact.get("context") or {}).get("planningPreview"))

    def _unassigned_pool_noise_count(self, artifact: dict[str, Any]) -> int:
        count = 0
        for event in (artifact.get("planningSteps") or []) + (artifact.get("toolEvents") or []):
            preview = ((event.get("metadata") or {}).get("resultPreview") or {}) if isinstance(event, dict) else {}
            for item in preview.get("unresolvedSlots") or []:
                pool_id = str((item or {}).get("poolId") or "")
                reason = str((item or {}).get("reason") or "")
                if pool_id.startswith("unassigned_") or reason == "no_intent_pool_candidate":
                    count += 1
        return count

    def _ordinary_meal_placeholders(self, days: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            segment
            for segment in self._segments(days)
            if str(segment.get("kind") or "") == "meal"
            and not self._is_concrete_amap_segment(segment)
            and self._grounding_status(segment) in ORDINARY_MEAL_STATUSES
            and "requiredGrounding=true" not in self._segment_text(segment)
        ]

    def _required_meals(self, days: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            segment
            for segment in self._segments(days)
            if str(segment.get("kind") or "") == "meal"
            and (
                "requiredGrounding=true" in self._segment_text(segment)
                or "explicit_food_experience" in self._segment_text(segment)
                or self._grounding_status(segment) in MEAL_PENDING_STATUSES
            )
        ]

    def _ordinary_meal_expandable(self, segment: dict[str, Any], pending_candidates: list[dict[str, Any]]) -> bool:
        if self._pending_for_segment(segment, pending_candidates, category="food"):
            return True
        return self._grounding_status(segment) in ORDINARY_MEAL_STATUSES

    def _required_meal_grounded_or_pending(
        self, segment: dict[str, Any], pending_candidates: list[dict[str, Any]]
    ) -> bool:
        return (
            self._is_concrete_amap_segment(segment)
            or self._pending_for_segment(segment, pending_candidates, category="food")
            or self._grounding_status(segment) in MEAL_PENDING_STATUSES
        )

    def _explicit_local_food_metrics(self, required_meals: list[dict[str, Any]]) -> dict[str, int]:
        generic = 0
        institutional = 0
        hotel = 0
        for segment in required_meals:
            if not self._is_concrete_amap_segment(segment):
                continue
            text = self._segment_text(segment)
            if INSTITUTIONAL_MEAL_RE.search(text):
                institutional += 1
            if HOTEL_MEAL_RE.search(text) and not LOCAL_FOOD_POSITIVE_RE.search(text):
                hotel += 1
            if not LOCAL_FOOD_POSITIVE_RE.search(text):
                generic += 1
        return {
            "explicitLocalFoodGenericCount": generic,
            "explicitLocalFoodNegativeRelevanceCount": generic + institutional + hotel,
            "institutionalMealCount": institutional,
            "hotelMealCount": hotel,
        }

    def _untrusted_meal_metrics(self, days: list[dict[str, Any]]) -> dict[str, int]:
        map_ready = 0
        routeable = 0
        for segment in self._segments(days):
            if not self._is_meal_segment(segment) or not self._is_mock_or_synthetic_segment(segment):
                continue
            if self._segment_claims_map_ready(segment):
                map_ready += 1
            if self._segment_claims_routeable(segment):
                routeable += 1
        return {
            "mockOrSyntheticMealMapReadyCount": map_ready,
            "mockOrSyntheticMealRouteableCount": routeable,
        }

    def _route_quality_metrics(self, artifact: dict[str, Any]) -> dict[str, Any]:
        check = self._route_quality_check(artifact)
        meal_high = 0
        meal_poor = 0
        max_day_km = 0.0
        max_meal_leg_km = 0.0
        for day in check.get("days") or []:
            if not isinstance(day, dict):
                continue
            issues = set(str(issue) for issue in day.get("issues") or [])
            max_day_km = max(max_day_km, float(day.get("totalRouteDistanceKm") or 0))
            if "meal_detour_high" in issues:
                meal_high += 1
            if "meal_detour_poor" in issues:
                meal_poor += 1
        for warning in check.get("warnings") or []:
            match = re.search(r"餐饮相邻路线偏绕：([0-9.]+)\s*km", str(warning))
            if match:
                max_meal_leg_km = max(max_meal_leg_km, float(match.group(1)))
        return {
            "mealDetourHighCount": meal_high + meal_poor,
            "mealDetourPoorCount": meal_poor,
            "maxTotalRouteKmPerDay": round(max_day_km, 1),
            "maxMealAdjacentLegKm": round(max_meal_leg_km, 1),
        }

    def _route_quality_check(self, artifact: dict[str, Any]) -> dict[str, Any]:
        report = artifact.get("verifierReport") or {}
        for check in report.get("checks") or []:
            if isinstance(check, dict) and check.get("name") == "route_quality_verifier":
                return check
        for event in reversed((artifact.get("planningSteps") or []) + (artifact.get("toolEvents") or [])):
            preview = ((event.get("metadata") or {}).get("resultPreview") or {}) if isinstance(event, dict) else {}
            route_verifier = preview.get("routeVerifier") if isinstance(preview.get("routeVerifier"), dict) else {}
            for check in route_verifier.get("checks") or []:
                if isinstance(check, dict) and check.get("name") == "route_quality_verifier":
                    return check
        return {}

    def _preferred_transport_from_text(self, text: str) -> str:
        if re.search(r"(公交|地铁|公共交通)", text):
            return "transit"
        if re.search(r"(骑行|自行车)", text):
            return "bicycling"
        if re.search(r"(步行|走路)", text):
            return "walking"
        if re.search(r"(打车|出租|网约车)", text):
            return "taxi"
        return ""

    def _preferred_route_mode_metrics(self, snapshot: dict[str, Any], preferred_mode: str) -> dict[str, Any]:
        preferred = self._normalize_route_mode(preferred_mode)
        non_preferred = 0
        substantive_non_preferred = 0
        missing_caveat = 0
        selected_count = 0
        selected_modes: set[str] = set()
        fallback_reasons: set[str] = set()
        for route in snapshot.get("routeOptions") or []:
            if not isinstance(route, dict) or not route.get("isSelected"):
                continue
            selected_count += 1
            mode = self._normalize_route_mode(str(route.get("mode") or route.get("transportMode") or ""))
            if mode:
                selected_modes.add(mode)
            if not preferred:
                continue
            if mode == preferred:
                continue
            non_preferred += 1
            if self._substantive_route_leg(route):
                substantive_non_preferred += 1
            payload = route.get("providerPayload") if isinstance(route.get("providerPayload"), dict) else {}
            fallback_reason = str(payload.get("fallbackReason") or "").strip()
            if fallback_reason:
                fallback_reasons.add(fallback_reason)
            if not payload.get("fallbackFromPreferredMode") or not payload.get("userVisibleCaveat"):
                missing_caveat += 1
        return {
            "selectedRouteCount": selected_count,
            "selectedRouteModes": sorted(selected_modes),
            "nonPreferredRouteFallbackReasons": sorted(fallback_reasons),
            "nonPreferredRouteModeCount": non_preferred,
            "nonPreferredRouteWithoutCaveatCount": missing_caveat,
            "nonPreferredRouteLegRatio": round(substantive_non_preferred / selected_count, 4)
            if selected_count
            else 0.0,
        }

    def _substantive_route_leg(self, route: dict[str, Any]) -> bool:
        distance_meters = self._numeric_route_field(route, "distanceMeters", "distance_meters")
        duration_minutes = self._numeric_route_field(route, "durationMinutes", "duration_minutes")
        if distance_meters is None and duration_minutes is None:
            return True
        mode = self._normalize_route_mode(str(route.get("mode") or route.get("transportMode") or ""))
        if mode == "walking" and distance_meters is not None and distance_meters <= 1500:
            return False
        if distance_meters is not None and distance_meters > 1500:
            return True
        if duration_minutes is not None and duration_minutes > 10:
            return True
        return False

    def _numeric_route_field(self, route: dict[str, Any], *keys: str) -> Optional[float]:
        for key in keys:
            value = route.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _normalize_route_mode(self, value: str) -> str:
        return {
            "public_transit": "transit",
            "transit": "transit",
            "bike": "bicycling",
            "cycling": "bicycling",
            "bicycling": "bicycling",
            "walk": "walking",
            "walking": "walking",
            "taxi": "taxi",
            "driving": "driving",
            "self_drive": "driving",
        }.get(str(value or ""), str(value or ""))

    def _risk_meal_search_count(self, snapshot: dict[str, Any]) -> int:
        kind_by_segment = {
            str(segment.get("id") or ""): str(segment.get("kind") or "")
            for segment in self._segments(snapshot.get("days") or [])
        }
        return sum(
            1
            for alert in snapshot.get("poiRiskAlerts") or []
            if kind_by_segment.get(str((alert or {}).get("segmentId") or "")) == "meal"
        )

    def _web_search_diagnostics_metrics(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        stable_providers = {
            "bocha-web-search",
            "tavily",
            "brave-web-search",
            "searxng",
            "ddgs",
            "google-cse",
            "cheetah-duckduckgo-html-search",
            "duckduckgo-html-search",
            "duckduckgo-lite-search",
            "baidu-html-search",
        }
        attempted: set[str] = set()
        successful: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        accepted_source_count = 0
        provider_diagnostics_present = False
        unavailable_all_failed = False
        queries: list[str] = []
        query_too_long_count = 0
        for alert in snapshot.get("poiRiskAlerts") or []:
            if not isinstance(alert, dict):
                continue
            alert_sources = alert.get("sources") if isinstance(alert.get("sources"), list) else []
            for source in alert_sources:
                if not isinstance(source, dict):
                    continue
                source_type = str(source.get("type") or "")
                if source_type == "webSearchProviderDiagnostics":
                    provider_diagnostics_present = True
                    attempted.update(str(item) for item in source.get("attemptedProviders") or [] if item)
                    successful.update(str(item) for item in source.get("successfulProviders") or [] if item)
                    failed.update(str(item) for item in source.get("failedProviders") or [] if item)
                    skipped.update(str(item) for item in source.get("skippedProviders") or [] if item)
                    if not source.get("successfulProviders") and (
                        source.get("failedProviders") or source.get("skippedProviders")
                    ):
                        unavailable_all_failed = True
                elif source_type == "riskSearchDiagnostics":
                    try:
                        accepted_source_count += int(source.get("acceptedSourceCount") or 0)
                    except (TypeError, ValueError):
                        pass
                    query = str(source.get("query") or "").strip()
                    if query:
                        queries.append(query)
                    try:
                        query_length = int(source.get("queryLength") or len(query))
                    except (TypeError, ValueError):
                        query_length = len(query)
                    if query_length > 180:
                        query_too_long_count += 1
        duplicate_query_count = max(0, len(queries) - len(set(queries)))
        configured_stable = bool((attempted | successful) & stable_providers)
        return {
            "webSearchAttemptedProviderCount": len(attempted),
            "webSearchAttemptedProviders": sorted(attempted),
            "webSearchSuccessfulProviderCount": len(successful),
            "webSearchSuccessfulProviders": sorted(successful),
            "webSearchFailedProviders": sorted(failed),
            "webSearchSkippedProviders": sorted(skipped),
            "webSearchAcceptedSourceCount": accepted_source_count,
            "webSearchProviderDiagnosticsPresent": provider_diagnostics_present,
            "riskSearchUnavailableBecauseAllProvidersFailed": unavailable_all_failed,
            "riskSearchHasAtLeastOneConfiguredStableProvider": configured_stable,
            "riskSearchQueryTooLongCount": query_too_long_count,
            "riskSearchDuplicateQueryCount": duplicate_query_count,
        }

    def _night_view_duplicate_family_count(self, days: list[dict[str, Any]]) -> int:
        seen: set[str] = set()
        duplicates = 0
        for segment in self._segments(days):
            text = self._segment_text(segment)
            if not re.search(r"(night_view|夜景|夜游|观景)", text, re.IGNORECASE):
                continue
            if self._is_pending_grounding_segment(segment):
                continue
            family = self._night_view_family(self._night_view_poi_text(segment) or text)
            if not family:
                continue
            if family in seen:
                duplicates += 1
            seen.add(family)
        return duplicates

    def _first_night_view_metrics(self, days: list[dict[str, Any]]) -> dict[str, Any]:
        for day in sorted(days, key=lambda item: int(item.get("dayNumber") or 0)):
            for segment in day.get("segments") or []:
                text = self._segment_text(segment)
                if not re.search(r"(night_view|夜景|夜游|观景)", text, re.IGNORECASE):
                    continue
                if self._is_pending_grounding_segment(segment):
                    continue
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                poi_text = self._night_view_poi_text(segment) or text
                return {
                    "firstNightPoi": str(poi.get("name") or ""),
                    "firstNightFamily": self._night_view_family(poi_text),
                }
        return {"firstNightPoi": "", "firstNightFamily": ""}

    def _is_pending_grounding_segment(self, segment: dict[str, Any]) -> bool:
        text = self._segment_text(segment)
        return any(
            token in text
            for token in (
                "groundingStatus：waiting_for_poi_grounding",
                "groundingStatus: waiting_for_poi_grounding",
                "groundingStatus：provider_rate_limited",
                "groundingStatus: provider_rate_limited",
            )
        )

    def _night_view_poi_text(self, segment: dict[str, Any]) -> str:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        return " ".join(
            str(value or "")
            for value in [
                poi.get("name"),
                poi.get("type"),
                poi.get("category"),
                poi.get("address"),
                poi.get("district"),
                poi.get("sourceNote") or poi.get("source_note"),
                poi.get("intentType") or poi.get("intent_type"),
            ]
        )

    def _night_view_family(self, text: str) -> str:
        normalized = re.sub(r"[\s\-_,.()（）·・，。；;:：]+", "", text.lower())
        family_aliases = [
            ("waterfront_evening", r"滨水|水岸|江边|河岸|湖岸|夜间游船"),
            ("historic_lit_street", r"历史街区|文化街区|胡同|老街|古街"),
            ("skyline_public_space", r"天际线|城市高点|公共高点|城市视野"),
            ("public_city_view", r"公共空间|城市夜景空间|广场|公园"),
            ("controlled_viewpoint", r"观景台|观景塔|登塔|预约|门票"),
        ]
        for family, pattern in family_aliases:
            if re.search(pattern, normalized):
                return family
        return re.sub(r"(广场|公园|景区|观景台|夜景|夜游|地标|商圈)$", "", normalized[:16])

    def _routeable_anchor_count(self, day: dict[str, Any]) -> int:
        count = 0
        for segment in day.get("segments") or []:
            kind = str(segment.get("kind") or "")
            if kind in {"meal", "rest", "note"} and not self._is_concrete_amap_segment(segment):
                continue
            if self._is_concrete_amap_segment(segment) or kind not in {"meal", "rest", "note"}:
                count += 1
        return count

    def _weak_night_view_count(self, days: list[dict[str, Any]]) -> int:
        count = 0
        for segment in self._segments(days):
            text = self._segment_text(segment)
            if re.search(r"(night_view|夜景|夜游|观景)", text, re.IGNORECASE) and WEAK_NIGHT_VIEW_RE.search(text):
                count += 1
        return count

    def _risk_low_without_source_count(self, snapshot: dict[str, Any]) -> int:
        count = 0
        for weather in snapshot.get("weatherSignals") or []:
            level = str(weather.get("riskLevel") or "").lower()
            status = str(weather.get("dataStatus") or "").lower()
            source = f"{weather.get('source') or ''} {weather.get('providerName') or ''}".lower()
            unreliable = (
                weather.get("fallbackUsed")
                or weather.get("failureReason")
                or status
                in {
                    "",
                    "fallback",
                    "unavailable",
                    "pending",
                    "not_checked",
                }
                or "mock" in source
            )
            visible_text = " ".join(
                str(weather.get(key) or "") for key in ("dailySummary", "purposeImpactReason", "userVisibleCaveat")
            )
            if level in LOW_LIKE_RISK and unreliable and self._text_exposes_low_risk(visible_text):
                count += 1
        for day in snapshot.get("days") or []:
            if self._text_exposes_low_risk(str(day.get("riskSummary") or "")) and "待" in str(
                day.get("riskSummary") or ""
            ):
                count += 1
        for route in snapshot.get("routeOptions") or []:
            level = str(route.get("crowdingRisk") or "").lower()
            source = str(route.get("source") or route.get("provider") or "").lower()
            route_status = str(route.get("routeStatus") or "").lower()
            unreliable = (
                "mock" in source
                or "unavailable" in source
                or route_status
                in {
                    "waiting_for_poi_grounding",
                    "provider_rate_limited",
                    "route_unavailable",
                }
            )
            if level in LOW_LIKE_RISK and unreliable:
                count += 1
        return count

    def _text_exposes_low_risk(self, text: str) -> bool:
        return bool(re.search(r"(低风险|适宜|影响较低|按计划出发)", text))

    def _route_quality_status(self, artifact: dict[str, Any]) -> str:
        report = artifact.get("verifierReport") or {}
        if isinstance(report.get("routeQualityStatus"), str):
            return report["routeQualityStatus"]
        metadata = report.get("metadata") if isinstance(report.get("metadata"), dict) else {}
        if isinstance(metadata.get("routeQualityStatus"), str):
            return metadata["routeQualityStatus"]
        for check in report.get("checks") or []:
            if isinstance(check, dict) and check.get("name") == "route_quality_verifier":
                return str(check.get("status") or "unknown")
        for event in reversed((artifact.get("planningSteps") or []) + (artifact.get("toolEvents") or [])):
            preview = ((event.get("metadata") or {}).get("resultPreview") or {}) if isinstance(event, dict) else {}
            route_verifier = preview.get("routeVerifier") if isinstance(preview.get("routeVerifier"), dict) else {}
            if isinstance(route_verifier.get("routeQualityStatus"), str):
                return route_verifier["routeQualityStatus"]
            for check in route_verifier.get("checks") or []:
                if isinstance(check, dict) and check.get("name") == "route_quality_verifier":
                    return str(check.get("status") or "unknown")
        return "unknown"

    def _pending_candidates(self, artifact: dict[str, Any], final_response: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = list(final_response.get("pendingPoiCandidates") or [])
        snapshot = artifact.get("sessionSnapshot") or {}
        candidates.extend(snapshot.get("pending_poi_candidates") or [])
        return candidates

    def _pending_for_segment(
        self, segment: dict[str, Any], pending_candidates: list[dict[str, Any]], *, category: str
    ) -> bool:
        segment_id = str(segment.get("id") or "")
        for candidate in pending_candidates:
            source_id = (
                candidate.get("sourceSegmentId") or candidate.get("source_segment_id") or candidate.get("segment_id")
            )
            if source_id == segment_id and str(candidate.get("category") or "") == category:
                return True
        return False

    def _is_concrete_amap_segment(self, segment: dict[str, Any]) -> bool:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        return bool(
            (poi.get("amapId") or poi.get("amap_id") or poi.get("id"))
            and str(poi.get("source") or "") == "amap-place-search"
            and self._valid_coordinate(poi.get("longitude"), poi.get("latitude"))
            and not self._is_mock_or_synthetic_segment(segment)
        )

    def _is_mock_or_synthetic_segment(self, segment: dict[str, Any]) -> bool:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        return self.poi_trust_policy.is_mock_or_synthetic_poi_values(
            amap_id=poi.get("amapId") or poi.get("amap_id") or poi.get("id"),
            source=poi.get("source"),
            source_note=poi.get("sourceNote") or poi.get("source_note"),
            name=poi.get("name"),
            kind=segment.get("kind"),
            intent_type=poi.get("intentType") or poi.get("intent_type"),
        )

    def _is_meal_segment(self, segment: dict[str, Any]) -> bool:
        if str(segment.get("kind") or "") == "meal":
            return True
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        return str(poi.get("intentType") or poi.get("intent_type") or "").lower() in {"meal", "dining", "food"}

    def _segment_claims_map_ready(self, segment: dict[str, Any]) -> bool:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        grounding = poi.get("grounding") if isinstance(poi.get("grounding"), dict) else {}
        return bool(
            grounding.get("mapReady") is True
            or poi.get("mapReady") is True
            or self._grounding_status(segment) == "agent_selected_candidate"
            or (
                (poi.get("amapId") or poi.get("amap_id") or poi.get("id"))
                and str(poi.get("source") or "") == "amap-place-search"
                and self._valid_coordinate(poi.get("longitude"), poi.get("latitude"))
            )
        )

    def _segment_claims_routeable(self, segment: dict[str, Any]) -> bool:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        grounding = poi.get("grounding") if isinstance(poi.get("grounding"), dict) else {}
        return bool(
            grounding.get("routeable") is True
            or poi.get("routeable") is True
            or segment.get("routeAnchor") is True
            or poi.get("routeAnchor") is True
            or self._segment_claims_map_ready(segment)
        )

    def _grounding_status(self, segment: dict[str, Any]) -> str:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        status = poi.get("groundingStatus") or (poi.get("grounding") or {}).get("groundingStatus")
        if status:
            return str(status)
        match = re.search(r"groundingStatus[：:=]\s*([A-Za-z0-9_]+)", self._segment_text(segment))
        return match.group(1) if match else ""

    def _segment_text(self, segment: dict[str, Any]) -> str:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        return " ".join(
            str(value or "")
            for value in [
                segment.get("kind"),
                segment.get("notes"),
                poi.get("name"),
                poi.get("type"),
                poi.get("category"),
                poi.get("address"),
                poi.get("district"),
                poi.get("sourceNote") or poi.get("source_note"),
                poi.get("intentType") or poi.get("intent_type"),
            ]
        )

    def _segments(self, days: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [segment for day in days for segment in (day.get("segments") or []) if isinstance(segment, dict)]

    def _valid_coordinate(self, longitude: object, latitude: object) -> bool:
        try:
            lon = float(longitude)
            lat = float(latitude)
        except (TypeError, ValueError):
            return False
        return lon != 0 and lat != 0

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        items: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                items.append(json.loads(line))
        return items

    def _safe_id(self, value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "scenario"

    def _namespace_seed_ids(self, snapshot: dict[str, Any], session_id: str) -> dict[str, Any]:
        """Avoid fixture-ID collisions while preserving a real existing itinerary shape."""
        namespaced = deepcopy(snapshot)
        suffix = re.sub(r"[^A-Za-z0-9]+", "", session_id)[-8:]

        def rewrite(value: Any) -> None:
            if isinstance(value, dict):
                if isinstance(value.get("id"), str) and value["id"]:
                    value["id"] = f"{value['id']}_{suffix}"
                for nested in value.values():
                    rewrite(nested)
            elif isinstance(value, list):
                for nested in value:
                    rewrite(nested)

        rewrite(namespaced)
        return namespaced

    def _mock_map_provider_config(self, payload: dict[str, Any], default: dict[str, Any]) -> dict[str, Any]:
        config = dict(default or {})
        scenario_config = payload.get("mockMapProvider") or payload.get("mock_map_provider")
        if isinstance(scenario_config, dict):
            config.update(scenario_config)
        return config

    def _should_default_mock_map_provider(self, scenario: dict[str, Any], mock_providers: bool) -> bool:
        if not mock_providers:
            return False
        if isinstance(scenario.get("mockMapProvider"), dict) or isinstance(scenario.get("mock_map_provider"), dict):
            return False
        expectations = scenario.get("expectations") or scenario.get("assertions") or {}
        if not isinstance(expectations, dict):
            return False
        candidate_first_keys = {
            "minRouteableAnchorsPerDay",
            "allowOrdinaryMealPlaceholder",
            "ordinaryMealMustBeExpandable",
            "explicitMealMustBeGroundedOrPending",
            "explicitLocalFoodMustBeRelevant",
            "forbidInstitutionalOrHotelMealsForLocalFood",
            "forbidPoorRouteQuality",
            "forbidMealDetourHigh",
            "forbidMockOrSyntheticMealsMapReady",
            "forbidMockOrSyntheticMealsRouteable",
            "maxTotalRouteKmPerDay",
            "maxMealAdjacentLegKm",
            "maxNonPreferredRouteLegRatio",
            "nonPreferredRouteRequiresCaveat",
            "riskSearchMustSkipOrdinaryMeals",
            "nightViewMustBeDistinctFamily",
            "forbidWeakNightViewPoi",
            "creativeStyleMustBeDeclared",
            "creativeVariantMustBePresent",
            "maxRepeatedPoiNames",
            "riskUnknownMustNotShowLow",
        }
        return any(key in expectations for key in candidate_first_keys)
