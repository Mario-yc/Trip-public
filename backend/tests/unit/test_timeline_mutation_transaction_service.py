import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.tests.intent_contract_support import IntentContractProviderMixin
from src.api.schemas.agent import AgentMessageContext, AgentMessageRequest, SelectedAgentChoiceRequest
from src.api.schemas.itinerary_patches import ItineraryPatchOperation
from src.core.database import PROJECT_ROOT
from src.runtime.recorded_amap_replay import recorded_amap_replay_scope
from src.models.route_option import RouteOption
from src.services.adjacent_insertion_candidate_service import AdjacentInsertionCandidateService
from src.services.agent_service import AgentService
from src.services.agent_verifier_service import AgentVerifierReport
from src.services.itinerary_service import ItineraryService
from src.services.itinerary_patch_service import ItineraryPatchService
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.map_poi_service import MapPoiService
from src.services.provider_route_insertion_service import (
    ProviderRouteInsertionService,
)
from src.services.timeline_mutation_intent_service import TimelineMutationIntentExtractor
from src.services.timeline_mutation_models import TimelineMutationDiff, TimelineMutationIntent
from src.services.timeline_mutation_poi_resolver import TimelineMutationPoiResolver
from src.services.timeline_mutation_postcondition_verifier import MutationPostconditionReport
from src.services.timeline_mutation_transaction_service import TimelineMutationTransactionService
from src.services.timeline_target_binder import TimelineTargetBinder

from timeline_mutation_test_support import (
    RecordedMapPoiService,
    append_second_day,
    campus_candidate,
    museum_candidate,
    open_db,
    server_route_decision_contract,
    seed_timeline,
)


class RecordedRouteService:
    """Route evidence fixture for mutation preflight; no network or synthetic success."""

    def build_routes(self, plan_id, pois, transport_mode="transit", segments=None, **_kwargs):
        has_candidate = str(plan_id).endswith("_incoming") or str(plan_id).endswith("_outgoing")
        return [
            RouteOption(
                id=f"route_{left.id}_{right.id}",
                plan_id=plan_id,
                from_segment_id=left.id,
                to_segment_id=right.id,
                from_poi_id=left.poi_id,
                to_poi_id=right.poi_id,
                distance_meters=400 if has_candidate else 750,
                duration_seconds=300 if has_candidate else 550,
                mode=transport_mode,
                provider="amap-webservice",
                is_selected=True,
                polyline=[
                    [pois[index].longitude, pois[index].latitude],
                    [pois[index + 1].longitude, pois[index + 1].latitude],
                ],
                provider_payload={
                    "walkingDistanceMeters": 0,
                    "transferCount": 0,
                    "waitSeconds": 0,
                    "riskPenaltyMinutes": 0,
                },
                queried_at=datetime.now(timezone.utc),
            )
            for index, (left, right) in enumerate(zip(segments or [], (segments or [])[1:]))
        ]


class FailingRecordedRouteService:
    def build_routes(self, *_args, **_kwargs):
        raise RuntimeError("recorded route provider unavailable")


def service(connection, map_service=None, **kwargs):
    map_provider = map_service or RecordedMapPoiService()
    resolver = TimelineMutationPoiResolver(
        map_poi_service=map_provider,
        adjacent_insertion_service=AdjacentInsertionCandidateService(map_provider, RecordedRouteService()),
    )
    if "patch_service" not in kwargs:
        patch_service = ItineraryPatchService(connection)
        patch_service.provider_route_insertion_service = ProviderRouteInsertionService(RecordedRouteService())
        kwargs["patch_service"] = patch_service
    return TimelineMutationTransactionService(connection, resolver=resolver, **kwargs)


def extract(text):
    return TimelineMutationIntentExtractor().extract(text, has_active_timeline=True)


class TimelineMutationControllerProvider(IntentContractProviderMixin):
    def __init__(self):
        self.controller_calls = 0
        self.tool_loop_calls = 0

    def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
        self.controller_calls += 1
        observation = context.get("observation") or {}
        if observation.get("lastOutcome"):
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "finish",
                "actionDirective": {"type": "finish", "assistantReply": "已核验本轮结果。"},
            }
        message = str(context.get("latestUserMessage") or context.get("effectiveUserMessage") or "")
        intent = extract(message).model_dump(by_alias=True)
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "patch_itinerary",
            "actionDirective": {
                "type": "patch_itinerary",
                "requestedOutcome": message,
                "mutationIntent": intent,
                "preserve": intent.get("preserve") or [],
                "maxChangedSegmentCount": 1,
            },
        }

    def run_tool_loop(self, *_args, **_kwargs):
        self.tool_loop_calls += 1
        raise AssertionError("simple timeline mutation must not enter generic tool loop")


def counts(connection, session_id):
    return {
        "versions": connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session_id,)
        ).fetchone()[0],
        "patches": connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session_id,)
        ).fetchone()[0],
    }


def test_baseline_museum_replace_is_one_patch_one_version_and_verified(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])
    recorded = RecordedMapPoiService()
    with open_db() as connection:
        session, base_version, _ = seed_timeline(connection)
        base_snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (base_version.id,),
            ).fetchone()["snapshot_json"]
        )
        base_snapshot["routeDecisionContract"] = {
            **base_snapshot["routeDecisionContract"],
            "schemaVersion": "route-decision-contract-v1",
            "status": "ready",
            "missingFields": [],
            "detourToleranceSource": "recorded_server_choice",
        }
        connection.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(base_snapshot, ensure_ascii=False), base_version.id),
        )
        connection.commit()
        before = counts(connection, session.session_id)
        outcome = service(connection, recorded).execute(
            session.session_id, extract("第一天美术馆改为清华美术馆"), source_turn_id="turn_test"
        )
        after = counts(connection, session.session_id)
        active = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session.session_id,)
        ).fetchone()[0]
        target = connection.execute(
            "SELECT p.amap_id, p.name FROM itinerary_segments s JOIN pois p ON p.id=s.poi_id WHERE s.id = ?",
            ("seg_831b98d1cb6e",),
        ).fetchone()
        active_snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (active,),
            ).fetchone()["snapshot_json"]
        )

    assert recorded.calls == [("北京", "清华美术馆", "all", 8)]
    assert outcome.status == "success"
    assert outcome.base_version_id == base_version.id
    assert outcome.target_segment_ids == ["seg_831b98d1cb6e"]
    assert outcome.postcondition_passed and outcome.structural_verifier_passed
    assert after["versions"] - before["versions"] == 1
    assert after["patches"] - before["patches"] == 1
    assert active == outcome.result_version_id
    assert tuple(target) == ("B0MUSEUM", "清华大学艺术博物馆")
    assert active_snapshot["routeDecisionContract"] == base_snapshot["routeDecisionContract"]
    event_names = [event["name"] for event in outcome.events]
    assert event_names.index("route_refresh_started") < event_names.index("route_refresh_completed")
    assert event_names.index("route_refresh_completed") < event_names.index("schedule_recomputed")
    assert event_names.index("schedule_recomputed") < event_names.index("structural_verifier")
    assert outcome.change_summary == {
        "dayNumber": 1,
        "beforeStartTime": "16:15",
        "beforeEndTime": "17:45",
        "afterStartTime": "16:15",
        "afterEndTime": "17:45",
        "beforePoiName": "美术馆",
        "afterPoiName": "清华大学艺术博物馆",
        "beforeTransportMode": "walking",
        "afterTransportMode": "walking",
    }


def test_model_semantic_add_preserves_existing_days_and_commits_exactly_one_node(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])

    class PassingStructuralVerifier:
        def verify_agent_write(self, *_args, **_kwargs):
            return AgentVerifierReport(passed=True)

    intent = TimelineMutationIntent.model_validate(
        {
            "operation": "add_segment",
            "selector": {"dayNumber": 2, "intentType": "campus_visit"},
            "replacement": {
                "poiQuery": "985大学",
                "startTime": "12:00",
                "durationMinutes": 60,
            },
            "sourceText": "在第二日追加一所符合要求的高校",
        }
    )
    with open_db() as connection:
        session, _, snapshot = seed_timeline(connection)
        _, before_snapshot = append_second_day(connection, session, snapshot)
        before_counts = counts(connection, session.session_id)
        before_day1_ids = [item["id"] for item in before_snapshot["days"][0]["segments"]]
        outcome = service(
            connection,
            RecordedMapPoiService([campus_candidate()]),
            structural_verifier=PassingStructuralVerifier(),
        ).execute(session.session_id, intent, source_turn_id="turn_add_campus")
        after_snapshot = ItinerarySnapshotService(connection).capture_snapshot(session.active_plan_id)
        after_counts = counts(connection, session.session_id)

    day1_ids = [item["id"] for item in after_snapshot["days"][0]["segments"]]
    day2_segments = after_snapshot["days"][1]["segments"]
    assert outcome.status == "success"
    assert after_counts["versions"] - before_counts["versions"] == 1
    assert after_counts["patches"] - before_counts["patches"] == 1
    assert day1_ids == before_day1_ids
    assert [item["id"] for item in day2_segments if item["id"] == "seg_day2_museum"] == ["seg_day2_museum"]
    added = next(item for item in day2_segments if item["id"] in outcome.direct_changed_segment_ids)
    assert added["poi"]["amapId"] == "B0PKU"
    assert added["semanticMetadata"]["intentType"] == "campus_visit"
    assert outcome.target_segment_ids == [added["id"]]
    assert outcome.postcondition_passed is True


def test_pending_slot_choice_commits_exact_slot_once_and_preserves_remaining_slots(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])

    class PassingStructuralVerifier:
        def verify_agent_write(self, *_args, **_kwargs):
            return AgentVerifierReport(passed=True)

    with open_db() as connection:
        session, _, snapshot = seed_timeline(connection)
        active_version, snapshot = append_second_day(connection, session, snapshot)
        target_slot = {
            "id": "pending_slot_day2_campus",
            "briefId": "brief_frozen",
            "poolId": "pool_day2_campus",
            "planningSlotId": "slot_day2_campus",
            "dayNumber": 2,
            "intentType": "campus_visit",
            "timeWindow": "afternoon",
            "startTime": "14:00",
            "durationMinutes": 120,
            "rawNeed": "Day 2 高校",
            "state": "pending",
        }
        remaining_slot = {
            "id": "pending_slot_day2_night",
            "briefId": "brief_frozen",
            "poolId": "pool_day2_night",
            "planningSlotId": "slot_day2_night",
            "dayNumber": 2,
            "intentType": "night_view",
            "timeWindow": "evening",
            "startTime": "20:00",
            "durationMinutes": 75,
            "rawNeed": "Day 2 夜景",
            "state": "pending",
        }
        snapshot.update(
            {
                "creativeBrief": {"briefId": "brief_frozen"},
                "portfolioSelectionContext": {
                    "planningSelectionRootTurnId": "turn_root",
                    "rootPortfolioId": "portfolio_root",
                    "focusBriefId": "brief_frozen",
                    "sourceUserTurnId": "turn_user",
                    "requestContractFingerprint": "request_fp",
                },
                "portfolioPartialTimeline": {"status": "partial", "pendingSlotCount": 2},
                "portfolioPendingSlots": [target_slot, remaining_slot],
            }
        )
        connection.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False, default=str), active_version.id),
        )
        connection.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'pending', ?, NULL, ?)
            """,
            (
                "candidate_record_pku",
                session.session_id,
                "turn_choice_source",
                "Day 2 高校",
                "北京",
                "campus",
                json.dumps([campus_candidate().model_dump(by_alias=True)], ensure_ascii=False),
                "2026-07-21T00:00:00+00:00",
            ),
        )
        connection.commit()
        intent = TimelineMutationIntent.model_validate(
            {
                "operation": "add_segment",
                "selector": {"dayNumber": 1, "intentType": "museum"},
                "replacement": {"poiQuery": "北京大学"},
                "source": "structured_ui",
                "sourceText": "选择 Day 2 北京大学",
                "pendingSlotSelection": {
                    "selectionSource": "user_chat_choice",
                    "planningSelectionRootTurnId": "turn_root",
                    "rootPortfolioId": "portfolio_root",
                    "focusBriefId": "brief_frozen",
                    "requestContractFingerprint": "request_fp",
                    "poolId": "pool_day2_campus",
                    "planningSlotId": "slot_day2_campus",
                    "dayNumber": 2,
                    "candidateRecordId": "candidate_record_pku",
                    "amapId": "B0PKU",
                    "baseVersionId": active_version.id,
                },
            }
        )
        mutation_service = service(
            connection,
            RecordedMapPoiService([campus_candidate()]),
            structural_verifier=PassingStructuralVerifier(),
        )
        before = counts(connection, session.session_id)
        first = mutation_service.execute(session.session_id, intent, source_turn_id="turn_choice")
        after_first = counts(connection, session.session_id)
        assert first.status == "success", (first.error_code, first.warnings, first.events)
        second = mutation_service.execute(session.session_id, intent, source_turn_id="turn_choice_duplicate")
        after_second = counts(connection, session.session_id)
        result_snapshot = mutation_service._version_snapshot(first.result_version_id, session.session_id)

    assert first.postcondition_passed is True
    assert after_first["versions"] - before["versions"] == 1
    assert after_first["patches"] - before["patches"] == 1
    assert second.result_version_id == first.result_version_id
    assert after_second == after_first
    assert len(result_snapshot["portfolioPendingSlots"]) == 1
    remaining = result_snapshot["portfolioPendingSlots"][0]
    assert {
        "briefId": remaining["briefId"],
        "poolId": remaining["poolId"],
        "planningSlotId": remaining["planningSlotId"],
        "dayNumber": remaining["dayNumber"],
    } == {
        "briefId": remaining_slot["briefId"],
        "poolId": remaining_slot["poolId"],
        "planningSlotId": remaining_slot["planningSlotId"],
        "dayNumber": remaining_slot["dayNumber"],
    }
    assert remaining["timingStatus"] == "time_pending"
    assert remaining["timingLabel"] == "时间待定"
    assert "startTime" not in remaining
    assert "endTime" not in remaining
    assert result_snapshot["portfolioPartialTimeline"]["pendingSlotCount"] == 1
    added = next(
        segment
        for day in result_snapshot["days"]
        for segment in day["segments"]
        if (segment.get("poi") or {}).get("amapId") == "B0PKU"
    )
    assert (
        added["semanticMetadata"]
        | {
            "creativeBriefId": "brief_frozen",
            "poolId": "pool_day2_campus",
            "planningSlotId": "slot_day2_campus",
            "manualPlacementSource": "user_chat_choice",
            "sourceCandidateRecordId": "candidate_record_pku",
        }
        == added["semanticMetadata"]
    )


def test_agent_model_semantic_add_uses_incremental_executor_instead_of_portfolio(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])

    class PassingStructuralVerifier:
        def verify_agent_write(self, *_args, **_kwargs):
            return AgentVerifierReport(passed=True)

    class SemanticAddControllerProvider(IntentContractProviderMixin):
        def __init__(self):
            self.controller_calls = 0
            self.tool_loop_calls = 0

        def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
            self.controller_calls += 1
            if (context.get("observation") or {}).get("lastOutcome"):
                return {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "finish",
                    "actionDirective": {"type": "finish", "assistantReply": "已完成针对性修改。"},
                }
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "patch_itinerary",
                "actionDirective": {
                    "type": "patch_itinerary",
                    "requestedOutcome": context.get("latestUserMessage") or "",
                    "mutationIntent": {
                        "operation": "add_segment",
                        "selector": {"dayNumber": 2, "intentType": "campus_visit"},
                        "replacement": {
                            "poiQuery": "985大学",
                            "startTime": "12:00",
                            "durationMinutes": 60,
                        },
                        "preserve": ["other_segments", "other_days", "user_locked_times"],
                        "source": "model_semantic_extractor",
                        "sourceText": context.get("latestUserMessage") or "",
                    },
                    "preserve": ["other_segments", "other_days", "user_locked_times"],
                    "maxChangedSegmentCount": 1,
                },
            }

        def run_tool_loop(self, *_args, **_kwargs):
            self.tool_loop_calls += 1
            raise AssertionError("targeted add must not enter staged portfolio or generic tool loop")

    with open_db() as connection:
        session, _, snapshot = seed_timeline(connection)
        append_second_day(connection, session, snapshot)
        provider = SemanticAddControllerProvider()
        mutation_service = service(
            connection,
            RecordedMapPoiService([campus_candidate()]),
            structural_verifier=PassingStructuralVerifier(),
        )
        response = AgentService(
            connection,
            provider=provider,
            timeline_mutation_service=mutation_service,
        ).send_message(
            session.session_id,
            AgentMessageRequest(content="第二天也要去参观985大学"),
        )
        snapshot_after = ItinerarySnapshotService(connection).capture_snapshot(session.active_plan_id)

    assert response.version is not None
    assert response.assistant_turn.timeline_mutation_outcome is not None
    assert response.assistant_turn.timeline_mutation_outcome["operation"] == "add_segment"
    assert response.assistant_turn.timeline_mutation_outcome["status"] == "success"
    assert provider.tool_loop_calls == 0
    assert len(snapshot_after["days"][0]["segments"]) == 4
    assert len(snapshot_after["days"][1]["segments"]) == 2
    assert {item["semanticMetadata"]["intentType"] for item in snapshot_after["days"][1]["segments"]} == {
        "museum",
        "campus_visit",
    }


def test_agent_controller_selects_transaction_executor_and_skips_generic_tool_loop(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])

    with open_db() as connection:
        session, _, _ = seed_timeline(connection)
        mutation_service = service(connection)
        provider = TimelineMutationControllerProvider()
        response = AgentService(
            connection,
            provider=provider,
            timeline_mutation_service=mutation_service,
        ).send_message(session.session_id, AgentMessageRequest(content="第一天美术馆改为清华美术馆"))
        request = json.loads(
            connection.execute(
                "SELECT agent_request_json FROM conversation_turns WHERE id = ?", (response.user_turn.id,)
            ).fetchone()[0]
        )
        persisted_target_times = connection.execute(
            "SELECT start_time, end_time FROM itinerary_segments WHERE id = ?",
            ("seg_831b98d1cb6e",),
        ).fetchone()

    assert response.version is not None
    assert response.agent_decision_count == 1
    assert provider.controller_calls == 1
    assert provider.tool_loop_calls == 0
    assert request["agentControlLoop"]["controlMetrics"]["localMutationBypassCount"] == 0
    assert not any(event.type in {"web_search", "ticket_lookup", "amap_weather"} for event in response.tool_events)
    assert "清华大学艺术博物馆" in response.assistant_turn.content
    assert "16:15-17:45" in response.assistant_turn.content
    assert "新版本" in response.assistant_turn.content
    assert tuple(persisted_target_times) == ("16:15", "17:45")


def test_ambiguous_not_found_semantic_mismatch_and_provider_failure_are_zero_write(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])
    with open_db() as connection:
        session, _, _ = seed_timeline(connection, duplicate_museum=True)
        before = counts(connection, session.session_id)
        ambiguous = service(connection).execute(session.session_id, extract("美术馆改为清华美术馆"))
        assert ambiguous.status == "needs_confirmation"
        assert len(ambiguous.options) == 3
        assert counts(connection, session.session_id) == before

    park = museum_candidate("B0PARK", "清华园")
    park.type = "风景名胜;公园"
    with open_db() as connection:
        session, _, _ = seed_timeline(connection)
        before = counts(connection, session.session_id)
        mismatch = service(connection, RecordedMapPoiService([park])).execute(
            session.session_id, extract("第一天美术馆改为清华美术馆")
        )
        provider = service(connection, RecordedMapPoiService(error=RuntimeError("AMap unavailable"))).execute(
            session.session_id, extract("第一天美术馆改为清华美术馆")
        )
        missing = service(connection).execute(session.session_id, extract("第一天颐和园改为圆明园"))
        assert counts(connection, session.session_id) == before

    assert mismatch.status == "no_change" and mismatch.error_code == "no_safe_candidate"
    assert provider.status == "no_change" and provider.error_code == "provider_failure"
    assert missing.status == "needs_confirmation" and missing.error_code == "target_not_found"


def test_multiple_material_candidates_require_confirmation(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])
    candidates = [museum_candidate("B0ONE", "清华大学艺术博物馆"), museum_candidate("B0TWO", "清华校史博物馆")]
    candidates[0].confidence = candidates[1].confidence = 0.95
    with open_db() as connection:
        session, _, _ = seed_timeline(connection)
        before = counts(connection, session.session_id)
        outcome = service(connection, RecordedMapPoiService(candidates)).execute(
            session.session_id, extract("第一天美术馆改为清华美术馆")
        )
        after = counts(connection, session.session_id)

    assert outcome.status == "needs_confirmation"
    assert outcome.error_code == "material_choice"
    assert len(outcome.options) == 3
    assert all(option.get("id") and option.get("label") and option.get("action") for option in outcome.options)
    assert outcome.options[-1]["kind"] == "custom_input"
    assert after == before


def test_noop_and_duplicate_retry_create_no_second_version(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])
    with open_db() as connection:
        session, _, _ = seed_timeline(connection, grounded_museum=True)
        before = counts(connection, session.session_id)
        noop = service(connection).execute(session.session_id, extract("第一天美术馆改为清华美术馆"))
        assert noop.status == "no_change"
        assert counts(connection, session.session_id) == before

    with open_db() as connection:
        session, _, _ = seed_timeline(connection)
        mutation_service = service(connection)
        first = mutation_service.execute(session.session_id, extract("第一天美术馆改为清华美术馆"))
        after_first = counts(connection, session.session_id)
        second = mutation_service.execute(session.session_id, extract("第一天美术馆改为清华美术馆"))
        after_second = counts(connection, session.session_id)

    assert first.status == "success"
    assert second.status == "no_change"
    assert after_second == after_first


def test_duplicate_remove_returns_existing_outcome_without_second_write(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])
    with open_db() as connection:
        session, _, _ = seed_timeline(connection)
        intent = extract("删除第一天的美术馆")
        mutation_service = service(connection)
        first = mutation_service.execute(session.session_id, intent)
        after_first = counts(connection, session.session_id)
        second = mutation_service.execute(session.session_id, intent)
        after_second = counts(connection, session.session_id)

    assert first.status == "success"
    assert second.status == "no_change"
    assert second.existing_outcome is True
    assert second.result_version_id == first.result_version_id
    assert after_second == after_first


@pytest.mark.parametrize(
    "stage",
    [
        "after target binding",
        "after candidate resolution",
        "after core patch",
        "after route refresh",
        "after schedule recompute",
        "after version save",
        "before structural verifier",
        "before postcondition verifier",
    ],
)
def test_failure_injection_preserves_or_restores_base_state(monkeypatch, stage):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])

    def inject(current):
        if current == stage:
            raise RuntimeError(f"injected at {stage}")

    with open_db() as connection:
        session, base_version, before_snapshot = seed_timeline(connection)
        before_counts = counts(connection, session.session_id)
        outcome = service(connection, failure_injector=inject).execute(
            session.session_id, extract("第一天美术馆改为清华美术馆")
        )
        active = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session.session_id,)
        ).fetchone()[0]
        current_snapshot = ItinerarySnapshotService(connection).capture_snapshot(session.active_plan_id)
        after_counts = counts(connection, session.session_id)

    assert outcome.status in {"failed", "rolled_back"}
    assert active == base_version.id
    assert stable_snapshot(current_snapshot) == stable_snapshot(before_snapshot)
    assert after_counts["versions"] == before_counts["versions"]
    assert after_counts["patches"] <= before_counts["patches"] + 1


def test_structural_and_postcondition_failure_roll_back(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])

    class FailsStructuralVerifier:
        def verify_agent_write(self, *_args, **_kwargs):
            return AgentVerifierReport(passed=False, hard_failures=["injected structural failure"])

    class FailingPostcondition:
        def verify(self, *_args, **_kwargs):
            return MutationPostconditionReport(
                False, TimelineMutationDiff(versionDelta=1), ["injected postcondition failure"]
            )

    with open_db() as connection:
        session, base, _ = seed_timeline(connection)
        structural = service(connection, structural_verifier=FailsStructuralVerifier()).execute(
            session.session_id, extract("第一天美术馆改为清华美术馆")
        )
        assert structural.status == "rolled_back"
        assert (
            connection.execute(
                "SELECT active_version_id FROM conversation_sessions WHERE id=?", (session.session_id,)
            ).fetchone()[0]
            == base.id
        )

    with open_db() as connection:
        session, base, _ = seed_timeline(connection)
        postcondition = service(connection, postcondition_verifier=FailingPostcondition()).execute(
            session.session_id, extract("第一天美术馆改为清华美术馆")
        )
        assert postcondition.status == "rolled_back"
        assert (
            connection.execute(
                "SELECT active_version_id FROM conversation_sessions WHERE id=?", (session.session_id,)
            ).fetchone()[0]
            == base.id
        )


def test_extra_owned_version_is_detected_and_removed(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])

    class ExtraVersionPatchService:
        def __init__(self, db):
            self.db = db
            self.real = ItineraryPatchService(db)

        def apply_patch(self, *args, **kwargs):
            response = self.real.apply_patch(*args, **kwargs)
            ItinerarySnapshotService(self.db).save_version(
                kwargs["planning_context"]["sessionId"],
                args[0],
                "injected_orphan",
                source_patch_id=response.patch.id,
                update_session=False,
            )
            self.db.commit()
            return response

    with open_db() as connection:
        session, base, _ = seed_timeline(connection)
        before = counts(connection, session.session_id)
        outcome = service(connection, patch_service=ExtraVersionPatchService(connection)).execute(
            session.session_id,
            extract("第一天美术馆改为清华美术馆"),
        )
        active = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session.session_id,)
        ).fetchone()[0]
        after = counts(connection, session.session_id)

    assert outcome.status == "rolled_back"
    assert outcome.error_code == "timeline_mutation_failed"
    assert active == base.id
    assert after["versions"] == before["versions"]
    assert after["patches"] == before["patches"] + 1


def test_stale_base_fails_closed_before_patch(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])

    class StaleBinder(TimelineTargetBinder):
        injected = False

        def assert_current(self, bound):
            if not self.injected:
                self.injected = True
                ItineraryPatchService(self.db).apply_patch(
                    bound.plan_id,
                    [ItineraryPatchOperation(op="replace_trip_title", value="并发胜出版本")],
                    source_type="concurrent_winner",
                    base_version_id=bound.base_version_id,
                )
            super().assert_current(bound)

    with open_db() as connection:
        session, base, _ = seed_timeline(connection)
        before = counts(connection, session.session_id)
        with pytest.raises(HTTPException) as error:
            service(connection, binder=StaleBinder(connection)).execute(
                session.session_id, extract("第一天美术馆改为清华美术馆")
            )
        after = counts(connection, session.session_id)
        active = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session.session_id,)
        ).fetchone()[0]
        winner = connection.execute(
            "SELECT validation_status, result_version_id FROM itinerary_patches WHERE source_type = 'concurrent_winner'"
        ).fetchone()
        title = connection.execute(
            "SELECT title FROM itinerary_plans WHERE id = ?", (session.active_plan_id,)
        ).fetchone()[0]

    assert error.value.status_code == 409
    assert after == {"versions": before["versions"] + 1, "patches": before["patches"] + 1}
    assert active != base.id and active == winner["result_version_id"]
    assert winner["validation_status"] == "accepted"
    assert title == "并发胜出版本"


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("删除第一天的美术馆", "remove_segment"),
        ("把第一天美术馆改到 15:30", "set_start_time"),
        ("第一天美术馆停留改为 2 小时", "set_duration"),
        ("第一天午餐到美术馆改为公交地铁", "set_transport_mode"),
    ],
)
def test_remove_time_duration_transport_share_transaction_channel(monkeypatch, text, operation):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])
    with open_db() as connection:
        session, _, _ = seed_timeline(
            connection,
            grounded_museum=operation != "remove_segment",
        )
        before = counts(connection, session.session_id)
        outcome = service(connection).execute(session.session_id, extract(text))
        after = counts(connection, session.session_id)

    assert outcome.status == "success"
    assert outcome.operation == operation
    assert after["versions"] - before["versions"] == 1
    assert after["patches"] - before["patches"] == 1


def test_route_provider_failure_is_zero_business_write_before_timeline_mutation(monkeypatch):
    def fail_routes(*_args, **_kwargs):
        raise RuntimeError("recorded route provider unavailable")

    monkeypatch.setattr(ItineraryService, "refresh_routes", fail_routes)
    with open_db() as connection:
        session, base, _ = seed_timeline(connection)
        connection.execute(
            """
            INSERT INTO route_options (
                id, plan_id, from_segment_id, to_segment_id, from_poi_id, to_poi_id,
                provider, mode, label, is_selected, sort_order, transport_mode,
                distance_meters, duration_seconds, duration_minutes, cost_amount,
                cost_currency, cost_estimate, crowding_risk, source, polyline_json,
                steps_json, provider_payload_json, error_json, queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "route_stale",
                session.active_plan_id,
                "seg_mid",
                "seg_831b98d1cb6e",
                "poi_seg_mid",
                "poi_seg_831b98d1cb6e",
                "amap",
                "walking",
                "旧路线",
                1,
                0,
                "walking",
                999,
                9999,
                167,
                0,
                "CNY",
                0,
                "unknown",
                "amap",
                "[[116.3,40.0]]",
                "[]",
                "{}",
                None,
                "2026-10-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (
                json.dumps(
                    ItinerarySnapshotService(connection).capture_snapshot(session.active_plan_id),
                    ensure_ascii=False,
                    default=str,
                ),
                base.id,
            ),
        )
        connection.commit()
        before = counts(connection, session.session_id)
        patch_service = ItineraryPatchService(connection)
        patch_service.provider_route_insertion_service = ProviderRouteInsertionService(FailingRecordedRouteService())
        outcome = service(connection, patch_service=patch_service).execute(
            session.session_id,
            extract("第一天美术馆改为清华美术馆"),
        )
        after = counts(connection, session.session_id)
        stale_count = connection.execute("SELECT COUNT(*) FROM route_options WHERE id = 'route_stale'").fetchone()[0]
        target = connection.execute(
            """
            SELECT p.amap_id, p.name
            FROM itinerary_segments s JOIN pois p ON p.id = s.poi_id
            WHERE s.id = 'seg_831b98d1cb6e'
            """
        ).fetchone()
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert outcome.status == "rolled_back"
    assert outcome.result_version_id is None
    assert after["versions"] == before["versions"]
    assert active_version_id == base.id
    assert tuple(target) == (None, "美术馆")
    assert stale_count == 1
    assert any("provider_route_matrix_preflight_failed" in warning for warning in outcome.warnings)


@pytest.mark.parametrize(
    "text",
    [
        "删除第一天的美术馆",
        "把第一天美术馆改到 15:30",
        "第一天美术馆停留改为 2 小时",
        "第一天午餐到美术馆改为公交地铁",
    ],
)
def test_timeline_topology_time_and_transport_provider_failure_are_zero_write(
    text,
):
    with open_db() as connection:
        session, base, before_snapshot = seed_timeline(
            connection,
            grounded_museum=True,
        )
        before = counts(connection, session.session_id)
        patch_service = ItineraryPatchService(connection)
        patch_service.provider_route_insertion_service = ProviderRouteInsertionService(FailingRecordedRouteService())

        outcome = service(connection, patch_service=patch_service).execute(
            session.session_id,
            extract(text),
        )
        after = counts(connection, session.session_id)
        after_snapshot = ItinerarySnapshotService(connection).capture_snapshot(session.active_plan_id)
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert outcome.status == "rolled_back"
    assert outcome.result_version_id is None
    assert after["versions"] == before["versions"]
    assert active_version_id == base.id
    assert stable_snapshot(after_snapshot) == stable_snapshot(before_snapshot)


def test_timeline_add_provider_failure_is_zero_write():
    intent = TimelineMutationIntent.model_validate(
        {
            "operation": "add_segment",
            "selector": {"dayNumber": 2, "intentType": "campus_visit"},
            "replacement": {
                "poiQuery": "985大学",
                "startTime": "12:00",
                "durationMinutes": 60,
            },
            "sourceText": "在第二日追加一所符合要求的高校",
        }
    )
    with open_db() as connection:
        session, _, snapshot = seed_timeline(connection)
        base, before_snapshot = append_second_day(
            connection,
            session,
            snapshot,
        )
        before = counts(connection, session.session_id)
        patch_service = ItineraryPatchService(connection)
        patch_service.provider_route_insertion_service = ProviderRouteInsertionService(FailingRecordedRouteService())

        outcome = service(
            connection,
            RecordedMapPoiService([campus_candidate()]),
            patch_service=patch_service,
        ).execute(session.session_id, intent)
        after = counts(connection, session.session_id)
        after_snapshot = ItinerarySnapshotService(connection).capture_snapshot(session.active_plan_id)
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert outcome.status == "rolled_back"
    assert outcome.result_version_id is None
    assert after["versions"] == before["versions"]
    assert active_version_id == base.id
    assert stable_snapshot(after_snapshot) == stable_snapshot(before_snapshot)


def test_transport_handoff_binds_exact_tsinghua_to_museum_pair(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])
    with open_db() as connection:
        session, base, _ = seed_timeline(connection, grounded_museum=True)
        connection.execute("DELETE FROM itinerary_segments WHERE id = 'seg_mid'")
        snapshot = ItinerarySnapshotService(connection).capture_snapshot(session.active_plan_id)
        snapshot["routeDecisionContract"] = server_route_decision_contract()
        connection.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False, default=str), base.id),
        )
        connection.commit()
        outcome = service(connection).execute(
            session.session_id,
            extract("第一天清华大学到美术馆改为公交地铁"),
        )

    assert outcome.status == "success"
    assert outcome.operation == "set_transport_mode"
    assert outcome.target_segment_ids == ["seg_before"]
    assert {tuple(pair) for pair in outcome.touched_route_pairs} == {
        ("seg_before", "seg_831b98d1cb6e"),
        ("seg_831b98d1cb6e", "seg_after"),
    }


def test_recorded_amap_replay_uses_production_parser(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])
    fixture = PROJECT_ROOT / "backend/evals/fixtures/beijing_amap_sanitized_recording.json"
    with recorded_amap_replay_scope(fixture) as replay:
        with open_db() as connection:
            session, _, _ = seed_timeline(connection)
            map_service = MapPoiService()
            resolver = TimelineMutationPoiResolver(
                map_poi_service=map_service,
                adjacent_insertion_service=AdjacentInsertionCandidateService(
                    map_service,
                    RecordedRouteService(),
                ),
            )
            patch_service = ItineraryPatchService(connection)
            patch_service.provider_route_insertion_service = ProviderRouteInsertionService(RecordedRouteService())
            outcome = TimelineMutationTransactionService(
                connection,
                resolver=resolver,
                patch_service=patch_service,
            ).execute(
                session.session_id,
                extract("第一天美术馆改为清华美术馆"),
            )

    assert outcome.status == "success"
    assert outcome.result_version_id
    assert replay.requests[0]["endpoint"] == "/v3/place/text"
    assert replay.requests[0]["params"]["keywords"] == "清华美术馆"


def test_material_choice_dispatches_back_into_transaction_without_controller(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])
    candidates = [museum_candidate("B0ONE", "清华大学艺术博物馆"), museum_candidate("B0TWO", "清华校史博物馆")]
    candidates[0].confidence = candidates[1].confidence = 0.95

    with open_db() as connection:
        session, _, _ = seed_timeline(connection)
        mutation_service = service(connection, RecordedMapPoiService(candidates))
        provider = TimelineMutationControllerProvider()
        agent = AgentService(connection, provider=provider, timeline_mutation_service=mutation_service)
        first = agent.send_message(session.session_id, AgentMessageRequest(content="第一天美术馆改为清华美术馆"))
        calls_after_first = provider.controller_calls
        option = first.assistant_turn.choice_options[0]
        second = agent.send_message(
            session.session_id,
            AgentMessageRequest(
                content="选择 Agent 选项",
                context=AgentMessageContext(
                    selectedAgentChoice=SelectedAgentChoiceRequest(
                        sourceAssistantTurnId=first.assistant_turn.id,
                        choiceId=option["id"],
                    )
                ),
            ),
        )

    assert first.terminal_status == "needs_confirmation"
    assert second.version is not None
    assert second.terminal_status == "success"
    assert second.assistant_turn.timeline_mutation_outcome["status"] == "success"
    assert calls_after_first == 2
    assert provider.controller_calls == calls_after_first


def test_ambiguous_target_choice_rebinds_exact_time_window_without_controller(monkeypatch):
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])

    with open_db() as connection:
        session, base, _ = seed_timeline(
            connection,
            duplicate_museum=True,
            grounded_museum=True,
        )
        connection.execute(
            """
            UPDATE pois
            SET amap_id = 'B0EXISTING', name = '美术馆'
            WHERE id = 'poi_seg_831b98d1cb6e'
            """
        )
        grounded_snapshot = ItinerarySnapshotService(connection).capture_snapshot(session.active_plan_id)
        grounded_snapshot["routeDecisionContract"] = server_route_decision_contract()
        connection.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (
                json.dumps(grounded_snapshot, ensure_ascii=False, default=str),
                base.id,
            ),
        )
        connection.commit()
        provider = TimelineMutationControllerProvider()
        agent = AgentService(
            connection,
            provider=provider,
            timeline_mutation_service=service(connection),
        )
        first = agent.send_message(session.session_id, AgentMessageRequest(content="美术馆改为清华美术馆"))
        calls_after_first = provider.controller_calls
        target_option = next(
            option for option in first.assistant_turn.choice_options if option.get("segmentId") == "seg_second_museum"
        )
        second = agent.send_message(
            session.session_id,
            AgentMessageRequest(
                content="选择 Agent 选项",
                context=AgentMessageContext(
                    selectedAgentChoice=SelectedAgentChoiceRequest(
                        sourceAssistantTurnId=first.assistant_turn.id,
                        choiceId=target_option["id"],
                    )
                ),
            ),
        )
        pois = {
            row["id"]: (row["amap_id"], row["name"])
            for row in connection.execute(
                """
                SELECT s.id, p.amap_id, p.name
                FROM itinerary_segments s JOIN pois p ON p.id = s.poi_id
                WHERE s.id IN ('seg_831b98d1cb6e', 'seg_second_museum')
                """
            ).fetchall()
        }

    assert first.terminal_status == "needs_confirmation"
    assert second.version is not None
    assert second.assistant_turn.timeline_mutation_outcome["targetSegmentIds"] == ["seg_second_museum"]
    assert pois["seg_second_museum"] == ("B0MUSEUM", "清华大学艺术博物馆")
    assert pois["seg_831b98d1cb6e"] == ("B0EXISTING", "美术馆")
    assert calls_after_first == 2
    assert provider.controller_calls == calls_after_first


def stable_snapshot(snapshot):
    value = json.loads(json.dumps(snapshot, ensure_ascii=False, default=str))
    value.pop("feasibilityReport", None)
    value.pop("scheduleDiagnostics", None)
    value.pop("onlineEnrichment", None)
    # Server-owned version metadata is not part of the mutable plan tables.
    value.pop("routeDecisionContract", None)
    return value
