import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import pytest

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.agent_verifier_service import AgentVerifierService
from src.services.route_insertion_scorer import RouteInsertionScorer


def _route_decision_contract() -> dict:
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="request_intent_contract",
        provenance={"sourceAssistantTurnId": "turn_verifier_test"},
        detour_tolerance={
            "maxGeneralizedCostDelta": 35,
            "maxDetourRatio": 0.35,
        },
        mobility_profile={
            "source": "verifier_test_request_contract",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
    )
    assert contract is not None
    return contract


def open_db() -> sqlite3.Connection:
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def test_verifier_accepts_grounded_amap_itinerary_and_patch_version_rows():
    with open_db() as db:
        ids = seed_plan(db)
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    assert report.passed is True
    assert report.hard_failures == []
    assert {check["name"] for check in report.checks} >= {
        "amap_poi_grounding",
        "pending_poi_not_final",
        "patch_version_invariant",
    }


def test_verifier_rejects_final_poi_without_amap_grounding():
    with open_db() as db:
        ids = seed_plan(db, poi_source="itinerary-skeleton", amap_id=None, confidence=0.0)
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    assert report.passed is False
    assert any("missing amapId" in failure for failure in report.hard_failures)
    assert any("source is itinerary-skeleton" in failure for failure in report.hard_failures)


def test_verifier_hard_fails_persisted_museum_semantic_mismatch():
    with open_db() as db:
        ids = seed_plan(db)
        row = db.execute("SELECT snapshot_json FROM itinerary_versions WHERE id = ?", (ids["version_id"],)).fetchone()
        snapshot = json.loads(row["snapshot_json"])
        poi = snapshot["days"][0]["segments"][0]["poi"]
        poi.update(
            {
                "name": "花海畔溪谷",
                "type": "风景名胜;风景名胜;风景名胜",
                "category": "scenic",
                "intentType": "museum",
                "groundingStatus": "agent_selected_candidate",
                "mapReady": True,
                "routeable": True,
                "needsConcretePoi": False,
            }
        )
        db.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False), ids["version_id"]),
        )
        db.commit()

        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"],
            ids["plan_id"],
            ids["version_id"],
            ids["patch_id"],
            planning_context={
                "requestIntentContract": {
                    "requiredIntents": [
                        {"intentType": "museum", "target": 1},
                        {
                            "intentType": "local_culture",
                            "target": 1,
                            "requiredMin": 0,
                            "requirementLevel": "soft_experience",
                        },
                    ]
                }
            },
        )

    semantic_check = next(check for check in report.checks if check["name"] == "intent_grounding_semantics_verifier")
    assert report.passed is False
    assert semantic_check["status"] == "failed"
    assert semantic_check["coverage"] == [
        {
            "intentType": "museum",
            "requiredMin": 1,
            "satisfiedCount": 0,
            "satisfiedBySegmentIds": [],
            "status": "unresolved",
        },
        {
            "intentType": "local_culture",
            "requiredMin": 0,
            "satisfiedCount": 0,
            "satisfiedBySegmentIds": [],
            "status": "covered",
        },
    ]
    assert any("intent.semantic.museum" in failure for failure in report.hard_failures)


def test_verifier_marks_agent_text_draft_grounding_not_passed_without_rollback_failure():
    with open_db() as db:
        ids = seed_plan(db, poi_source="agent-text-timeline", amap_id=None, confidence=0.4)
        db.execute("UPDATE pois SET latitude = NULL, longitude = NULL WHERE plan_id = ?", (ids["plan_id"],))
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    grounding_check = next(check for check in report.checks if check["name"] == "amap_poi_grounding")
    assert report.passed is True
    assert report.hard_failures == []
    assert grounding_check["status"] == "failed"
    assert grounding_check["mapReady"] is False
    assert grounding_check["draft"]


def test_verifier_marks_agent_text_draft_grounding_warning_in_draft_mode():
    with open_db() as db:
        ids = seed_plan(db, poi_source="agent-text-timeline", amap_id=None, confidence=0.4)
        db.execute("UPDATE pois SET latitude = NULL, longitude = NULL WHERE plan_id = ?", (ids["plan_id"],))
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"],
            ids["plan_id"],
            ids["version_id"],
            ids["patch_id"],
            planning_context={
                "candidateFirstGrounding": {
                    "finalization": {
                        "canCreateVersion": True,
                        "unresolvedPolicy": "persist_viable_partial_days",
                        "draftPersistenceOverride": True,
                    }
                }
            },
        )

    grounding_check = next(check for check in report.checks if check["name"] == "amap_poi_grounding")
    assert report.passed is True
    assert report.hard_failures == []
    assert grounding_check["status"] == "warning"
    assert grounding_check["draftPersistenceMode"] is True
    assert grounding_check["mapReady"] is False


def test_schedule_verifier_rejects_any_overlapping_segment_intervals():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            "UPDATE itinerary_segments SET start_time = ?, end_time = ? WHERE id = ?",
            ("11:26", "12:56", "seg_verifier"),
        )
        db.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude, photo_url,
                source, confidence, amap_id, type, district, address, source_note,
                source_url, photos_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "poi_overlap",
                ids["plan_id"],
                "午餐",
                "北京",
                "food",
                39.9,
                116.4,
                None,
                "amap-place-search",
                0.9,
                "amap_overlap",
                "餐饮服务",
                "",
                "",
                "groundingStatus：agent_selected_candidate",
                None,
                "[]",
            ),
        )
        db.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time, end_time,
                poi_id, transport_mode, estimated_cost, notes,
                weather_signal_id, traffic_crowding_signal_id, ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "seg_overlap",
                ids["plan_id"],
                "day_verifier",
                2,
                "meal",
                "12:00",
                "13:15",
                "poi_overlap",
                "transit",
                0,
                "",
                None,
                None,
                None,
            ),
        )
        db.commit()

        report = AgentVerifierService(db).verify_schedule_feasibility(ids["plan_id"])

    assert report.passed is False
    assert report.metadata["scheduleOverlapCount"] == 1
    assert report.metadata["scheduleOverlapMinutes"] == 56
    assert report.metadata["scheduleStatus"] == "conflict"
    assert any("schedule.overlap" in failure for failure in report.hard_failures)


def test_schedule_verifier_rejects_segment_order_that_is_not_chronological():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            "UPDATE itinerary_segments SET start_time = ?, end_time = ? WHERE id = ?",
            ("12:00", "13:00", "seg_verifier"),
        )
        db.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude, photo_url,
                source, confidence, amap_id, type, district, address, source_note,
                source_url, photos_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "poi_earlier",
                ids["plan_id"],
                "更早活动",
                "北京",
                "scenic",
                39.91,
                116.41,
                None,
                "amap-place-search",
                0.9,
                "amap_earlier",
                "风景名胜",
                "",
                "",
                "groundingStatus：agent_selected_candidate",
                None,
                "[]",
            ),
        )
        db.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time, end_time,
                poi_id, transport_mode, estimated_cost, notes,
                weather_signal_id, traffic_crowding_signal_id, ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "seg_earlier",
                ids["plan_id"],
                "day_verifier",
                2,
                "activity",
                "09:00",
                "10:00",
                "poi_earlier",
                "transit",
                0,
                "",
                None,
                None,
                None,
            ),
        )
        db.commit()

        report = AgentVerifierService(db).verify_schedule_feasibility(ids["plan_id"])

    assert report.passed is False
    assert report.metadata["chronologyViolationCount"] == 1
    assert any("schedule.chronology" in failure for failure in report.hard_failures)


def test_agent_write_verifier_rejects_inverted_budget_breakdown_snapshot():
    with open_db() as db:
        ids = seed_plan(db)
        row = db.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (ids["version_id"],),
        ).fetchone()
        snapshot = json.loads(row["snapshot_json"])
        snapshot["budgetBreakdown"] = {
            "knownTotal": 411,
            "provisionalMin": 261,
            "provisionalPreferred": 341,
            "provisionalMax": 421,
        }
        db.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False), ids["version_id"]),
        )
        db.commit()

        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    assert report.passed is False
    budget_check = next(check for check in report.checks if check["name"] == "budget_invariant")
    assert budget_check["invariantValid"] is False
    assert any("budget.invariant" in failure for failure in report.hard_failures)


def test_map_readiness_verifier_fails_for_draft_only_poi():
    with open_db() as db:
        ids = seed_plan(db, poi_source="agent-text-timeline", amap_id=None, confidence=0.4)
        db.execute("UPDATE pois SET latitude = NULL, longitude = NULL WHERE plan_id = ?", (ids["plan_id"],))
        db.commit()
        report = AgentVerifierService(db).verify_map_readiness(ids["plan_id"])

    check = report.checks[0]
    assert report.passed is False
    assert check["name"] == "map_readiness_verifier"
    assert check["mapReady"] is False
    assert "missing providerPoiId/coordinates" in report.hard_failures[0]


def test_map_readiness_verifier_rejects_non_985_campus_from_snapshot_constraint_without_marker():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            "UPDATE pois SET name = '北京交通大学', type = '科教文化服务;学校;高等院校', category = 'campus' WHERE plan_id = ?",
            (ids["plan_id"],),
        )
        row = db.execute("SELECT snapshot_json FROM itinerary_versions WHERE id = ?", (ids["version_id"],)).fetchone()
        snapshot = json.loads(row["snapshot_json"])
        snapshot["hardConstraints"] = {
            "campusTier": {
                "value": "985",
                "strict": True,
                "source": "explicit_user_request",
                "expectedCount": 1,
            }
        }
        db.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False), ids["version_id"]),
        )
        db.commit()
        report = AgentVerifierService(db).verify_map_readiness(ids["plan_id"])
        write_report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    hard_check = next(check for check in report.checks if check["name"] == "hard_requirement_coverage")
    assert report.passed is False
    assert hard_check["campusTier"] == "985"
    assert hard_check["checkedCampusCount"] == 1
    assert hard_check["constraintSource"] == "explicit_user_request"
    assert hard_check["non985CampusNames"] == ["北京交通大学"]
    assert any("hard_requirement_coverage" in failure for failure in report.hard_failures)
    assert write_report.passed is False
    assert any("non-985" in failure for failure in write_report.hard_failures)


def test_draft_verifier_keeps_unresolved_985_slot_pending_without_calling_it_non_985():
    with open_db() as db:
        ids = seed_plan(db, poi_source="agent-text-timeline", amap_id=None, confidence=0.35)
        db.execute(
            """
            UPDATE pois
            SET name = '985高校参观', type = '待高德候选补全', category = 'campus',
                source_note = 'groundingStatus：waiting_for_poi_grounding；intentType：campus_visit；高德 POI 待校验'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.execute(
            """
            UPDATE itinerary_segments
            SET notes = 'groundingStatus：waiting_for_poi_grounding；intentType：campus_visit；needsConcretePoi=true'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"],
            ids["plan_id"],
            ids["version_id"],
            ids["patch_id"],
            planning_context={
                "hardConstraints": {
                    "campusTier": {
                        "value": "985",
                        "strict": True,
                        "source": "explicit_user_request",
                        "expectedCount": 1,
                    }
                },
                "candidateFirstGrounding": {
                    "finalization": {
                        "canCreateVersion": True,
                        "unresolvedPolicy": "persist_viable_partial_days",
                        "draftPersistenceOverride": True,
                    }
                },
            },
        )

    hard_check = next(check for check in report.checks if check["name"] == "hard_requirement_coverage")
    assert report.passed is True
    assert hard_check["status"] == "pending"
    assert hard_check["checkedCampusCount"] == 0
    assert hard_check["unresolvedCampusCount"] == 1
    assert hard_check["missingCampusCount"] == 1
    assert hard_check["non985CampusNames"] == []
    assert hard_check["hardConstraintRelaxed"] is False
    assert not any("non-985" in failure for failure in report.hard_failures)


def test_verifier_rejects_mock_amap_poi_as_map_ready_in_complete_mode():
    with open_db() as db:
        ids = seed_plan(db, amap_id="mock_amap_abc", confidence=0.92)
        db.execute(
            """
            UPDATE pois
            SET name = '北京本地菜餐厅',
                category = 'food',
                source_note = 'CLI eval mock AMap candidate; only used when mockMapProvider is enabled. groundingStatus：agent_selected_candidate；intentType：meal'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.execute(
            "UPDATE itinerary_segments SET kind = 'meal', semantic_metadata_json = ? WHERE plan_id = ?",
            (
                json.dumps(
                    {
                        "intentType": "meal",
                        "routeAnchor": True,
                        "groundingStatus": "agent_selected_candidate",
                        "required": True,
                    }
                ),
                ids["plan_id"],
            ),
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    grounding_check = next(check for check in report.checks if check["name"] == "amap_poi_grounding")
    assert report.passed is False
    assert grounding_check["status"] == "failed"
    assert any("mock_or_synthetic_poi" in failure for failure in report.hard_failures)


def test_verifier_warns_for_mock_amap_poi_in_draft_mode():
    with open_db() as db:
        ids = seed_plan(db, amap_id="mock_amap_abc", confidence=0.92)
        db.execute(
            """
            UPDATE pois
            SET name = '北京本地菜餐厅',
                category = 'food',
                source_note = 'CLI eval mock AMap candidate; only used when mockMapProvider is enabled. groundingStatus：agent_selected_candidate；intentType：meal'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.execute(
            "UPDATE itinerary_segments SET kind = 'meal', semantic_metadata_json = ? WHERE plan_id = ?",
            (
                json.dumps(
                    {
                        "intentType": "meal",
                        "routeAnchor": True,
                        "groundingStatus": "agent_selected_candidate",
                        "required": True,
                    }
                ),
                ids["plan_id"],
            ),
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"],
            ids["plan_id"],
            ids["version_id"],
            ids["patch_id"],
            planning_context={
                "candidateFirstGrounding": {
                    "finalization": {
                        "canCreateVersion": True,
                        "unresolvedPolicy": "persist_viable_partial_days",
                        "draftPersistenceOverride": True,
                    }
                }
            },
        )

    grounding_check = next(check for check in report.checks if check["name"] == "amap_poi_grounding")
    assert report.passed is True
    assert grounding_check["status"] == "warning"
    assert any("mock_or_synthetic_poi" in item for item in grounding_check["draft"])


def test_route_feasibility_verifier_allows_single_segment_itinerary():
    with open_db() as db:
        ids = seed_plan(db)
        report = AgentVerifierService(db).verify_route_feasibility(ids["plan_id"])

    check = report.checks[0]
    assert report.passed is True
    assert check["checkedRoutes"] == 0


def test_candidate_first_expected_dates_keep_partial_viable_days():
    service = AgentVerifierService(sqlite3.connect(":memory:"))
    expected_dates = ["2026-10-01", "2026-10-02"]

    result = service._candidate_first_expected_dates(
        {
            "candidateFirstGrounding": {
                "unresolvedDays": [2],
                "finalization": {
                    "canCreateVersion": True,
                    "unresolvedPolicy": "persist_viable_partial_days",
                },
            }
        },
        expected_dates,
        actual_dates=expected_dates,
    )

    assert result == expected_dates


def test_candidate_first_expected_dates_trim_unresolved_days_without_partial_policy():
    service = AgentVerifierService(sqlite3.connect(":memory:"))

    result = service._candidate_first_expected_dates(
        {
            "candidateFirstGrounding": {
                "unresolvedDays": [2],
                "finalization": {
                    "canCreateVersion": False,
                    "unresolvedPolicy": "no_minimum_viable_day",
                },
            }
        },
        ["2026-10-01", "2026-10-02"],
    )

    assert result == ["2026-10-01"]


def test_map_readiness_ignores_optional_meal_draft_poi():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            """
            UPDATE itinerary_segments
            SET kind = 'meal', semantic_metadata_json = '{"intentType":"meal","routeAnchor":false,"groundingStatus":"not_required","required":false}'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.execute(
            """
            UPDATE pois
            SET source = 'agent-text-timeline', amap_id = NULL, latitude = NULL, longitude = NULL,
                confidence = 0.35, source_note = 'groundingStatus：not_required；routeAnchor=false'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.commit()
        report = AgentVerifierService(db).verify_map_readiness(ids["plan_id"])

    check = report.checks[0]
    assert report.passed is True
    assert check["requiredMapReady"] is True
    assert check["optionalMapReady"] is True


def test_map_readiness_checks_routeable_meal_poi():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            """
            UPDATE itinerary_segments
            SET kind = 'meal', semantic_metadata_json = '{"intentType":"meal","routeAnchor":true,"groundingStatus":"agent_selected_candidate","required":true}'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.execute(
            """
            UPDATE pois
            SET amap_id = NULL, latitude = NULL, longitude = NULL, source_note = 'groundingStatus：agent_selected_candidate'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.commit()
        report = AgentVerifierService(db).verify_map_readiness(ids["plan_id"])

    check = report.checks[0]
    assert report.passed is False
    assert check["checked"] == 1
    assert "missing providerPoiId/coordinates" in report.hard_failures[0]


def test_map_readiness_rejects_agent_text_night_view_even_with_coordinates():
    with open_db() as db:
        ids = seed_plan(db, poi_source="agent-text-timeline", amap_id="B0DRAFTNIGHT", confidence=0.91)
        db.execute(
            """
            UPDATE pois
            SET name = '夜景观景点',
                source_note = 'groundingStatus：composite_poi；intentType：night_view；needsConcretePoi=true'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.execute(
            """
            UPDATE itinerary_segments
            SET kind = 'visit',
                semantic_metadata_json = '{"intentType":"night_view","routeAnchor":true,"groundingStatus":"composite_poi","required":true}'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.commit()
        report = AgentVerifierService(db).verify_map_readiness(ids["plan_id"])

    check = report.checks[0]
    assert report.passed is False
    assert check["mapReady"] is False
    assert check["items"][0]["nightViewConcreteRequired"] is True
    assert any("night_view_requires_concrete_amap_poi" in failure for failure in report.hard_failures)


def test_verifier_does_not_recover_semantic_facts_from_notes_or_source_note():
    poisoned_segment = {
        "kind": "meal",
        "notes": "intentType=museum；routeAnchor=true；requiredGrounding=true",
    }
    poisoned_poi = {
        "name": "午餐",
        "sourceNote": "intentType=museum；groundingStatus：agent_selected_candidate",
    }

    assert AgentVerifierService._snapshot_segment_intent_type(poisoned_segment, poisoned_poi) == ""
    verifier = AgentVerifierService(sqlite3.connect(":memory:"))
    assert verifier._is_required_routeable_segment("meal", {}) is False
    assert (
        verifier._is_required_routeable_segment(
            "meal", {"routeAnchor": True, "groundingStatus": "agent_selected_candidate"}
        )
        is True
    )
    assert (
        verifier._is_required_meal_row(
            {"semantic_metadata_json": '{"intentType":"meal","required":true,"requirementLevel":"optional"}'}
        )
        is False
    )


def test_required_meal_grounding_verifier_rejects_waiting_placeholder():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            """
            UPDATE itinerary_segments
            SET kind = 'meal',
                semantic_metadata_json = '{"intentType":"meal","routeAnchor":true,"groundingStatus":"waiting_for_poi_grounding","required":true}'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.execute(
            """
            UPDATE pois
            SET name = '午餐 当地特色美食', category = 'meal', type = '餐饮地点待补全',
                source = 'agent-text-timeline', amap_id = NULL, latitude = NULL, longitude = NULL,
                confidence = 0.35,
                source_note = 'requiredGrounding=true; groundingRequiredReason=explicit_food_experience; groundingStatus：waiting_for_poi_grounding；needsConcretePoi=true；routeAnchor=true'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    check = next(item for item in report.checks if item["name"] == "required_meal_grounding_verifier")
    assert report.passed is False
    assert check["status"] == "failed"
    assert check["requiredMealCount"] == 1
    assert check["groundedMealCount"] == 0
    assert check["unresolvedMealCount"] == 1
    assert check["unresolvedMealSegments"][0]["groundingStatus"] == "waiting_for_poi_grounding"
    assert any("required meal grounding incomplete" in failure for failure in report.hard_failures)


def test_required_meal_grounding_verifier_soft_warns_for_partial_candidate_first_draft():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            """
            UPDATE itinerary_segments
            SET kind = 'meal',
                semantic_metadata_json = '{"intentType":"meal","routeAnchor":true,"groundingStatus":"waiting_for_poi_grounding","required":true}'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.execute(
            """
            UPDATE pois
            SET name = '晚餐 当地特色美食', category = 'meal', type = '餐饮地点待补全',
                source = 'agent-text-timeline', amap_id = NULL, latitude = NULL, longitude = NULL,
                confidence = 0.35,
                source_note = 'requiredGrounding=true; groundingRequiredReason=explicit_food_experience; groundingStatus：waiting_for_poi_grounding；needsConcretePoi=true；routeAnchor=true'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"],
            ids["plan_id"],
            ids["version_id"],
            ids["patch_id"],
            planning_context={
                "candidateFirstGrounding": {
                    "finalization": {
                        "canCreateVersion": True,
                        "unresolvedPolicy": "persist_viable_partial_days",
                    },
                    "requiredIntentCoverage": [
                        {
                            "intentType": "meal",
                            "requiredIntent": True,
                            "targetCount": 4,
                            "selectedCount": 2,
                            "missingCount": 2,
                            "coverageStatus": "partial",
                        }
                    ],
                }
            },
        )

    check = next(item for item in report.checks if item["name"] == "required_meal_grounding_verifier")
    assert report.passed is True
    assert check["status"] == "warning"
    assert check["partialDraftAllowed"] is True
    assert check["unresolvedMealCount"] == 1
    assert not any("required meal grounding incomplete:" in failure for failure in report.hard_failures)
    assert any(
        "required meal grounding incomplete in partial editable draft" in failure for failure in report.soft_failures
    )


def test_required_meal_grounding_verifier_soft_warns_when_all_meals_persisted_pending():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            """
            UPDATE itinerary_segments
            SET kind = 'meal',
                semantic_metadata_json = '{"intentType":"meal","routeAnchor":true,"groundingStatus":"waiting_for_poi_grounding","required":true}'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.execute(
            """
            UPDATE pois
            SET name = '午餐 当地特色美食', category = 'meal', type = '餐饮地点待补全',
                source = 'agent-text-timeline', amap_id = NULL, latitude = NULL, longitude = NULL,
                confidence = 0.35,
                source_note = 'requiredGrounding=true; groundingRequiredReason=explicit_food_experience; groundingStatus：waiting_for_poi_grounding；needsConcretePoi=true；routeAnchor=true'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"],
            ids["plan_id"],
            ids["version_id"],
            ids["patch_id"],
            planning_context={
                "candidateFirstGrounding": {
                    "finalization": {
                        "canCreateVersion": True,
                        "unresolvedPolicy": "persist_viable_partial_days",
                    },
                    "requiredIntentCoverage": [
                        {
                            "intentType": "meal",
                            "requiredIntent": True,
                            "targetCount": 1,
                            "selectedCount": 0,
                            "missingCount": 1,
                            "coverageStatus": "missing",
                        }
                    ],
                    "unresolvedSlots": [
                        {
                            "slotId": "day1_lunch",
                            "kind": "meal",
                            "intentType": "meal",
                            "reason": "budget_exceeded",
                            "state": "waiting_for_poi_grounding",
                            "persistedPending": True,
                        }
                    ],
                }
            },
        )

    check = next(item for item in report.checks if item["name"] == "required_meal_grounding_verifier")
    assert report.passed is True
    assert check["status"] == "warning"
    assert check["partialDraftAllowed"] is True
    assert check["unresolvedMealCount"] == 1
    assert report.hard_failures == []
    assert any("partial editable draft" in failure for failure in report.soft_failures)


def test_required_meal_grounding_verifier_rejects_missing_persisted_pending_evidence():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            """
            UPDATE itinerary_segments
            SET kind = 'meal',
                semantic_metadata_json = '{"intentType":"meal","routeAnchor":true,"groundingStatus":"waiting_for_poi_grounding","required":true}'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.execute(
            """
            UPDATE pois
            SET name = '午餐 当地特色美食', category = 'meal', type = '餐饮地点待补全',
                source = 'agent-text-timeline', amap_id = NULL, latitude = NULL, longitude = NULL,
                confidence = 0.35,
                source_note = 'requiredGrounding=true; groundingRequiredReason=explicit_food_experience; groundingStatus：waiting_for_poi_grounding；needsConcretePoi=true；routeAnchor=true'
            WHERE plan_id = ?
            """,
            (ids["plan_id"],),
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"],
            ids["plan_id"],
            ids["version_id"],
            ids["patch_id"],
            planning_context={
                "candidateFirstGrounding": {
                    "finalization": {
                        "canCreateVersion": True,
                        "unresolvedPolicy": "persist_viable_partial_days",
                    },
                    "requiredIntentCoverage": [
                        {
                            "intentType": "meal",
                            "requiredIntent": True,
                            "targetCount": 1,
                            "selectedCount": 0,
                            "missingCount": 1,
                            "coverageStatus": "missing",
                        }
                    ],
                    "unresolvedSlots": [
                        {
                            "slotId": "day1_lunch",
                            "kind": "meal",
                            "intentType": "meal",
                            "reason": "budget_exceeded",
                            "state": "waiting_for_poi_grounding",
                        }
                    ],
                }
            },
        )

    check = next(item for item in report.checks if item["name"] == "required_meal_grounding_verifier")
    assert report.passed is False
    assert check["status"] == "failed"
    assert check["partialDraftAllowed"] is False
    assert any("required meal grounding incomplete" in failure for failure in report.hard_failures)


def test_route_feasibility_reports_route_quality_warning_for_long_meal_leg():
    with open_db() as db:
        ids = seed_plan(db)
        timestamp = now()
        db.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude, photo_url, source, confidence,
                amap_id, type, district, address, source_note, source_url, photos_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "poi_meal_quality",
                ids["plan_id"],
                "样例餐饮地点",
                "北京",
                "food",
                39.5,
                116.8,
                None,
                "amap-place-search",
                0.91,
                "B000MEAL",
                "餐饮服务;中餐厅",
                "示例区",
                "测试路",
                "groundingStatus：agent_selected_candidate",
                None,
                "[]",
            ),
        )
        db.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time, end_time, poi_id,
                transport_mode, estimated_cost, semantic_metadata_json, notes, weather_signal_id, traffic_crowding_signal_id,
                ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "seg_meal_quality",
                ids["plan_id"],
                "day_verifier",
                2,
                "meal",
                "12:00",
                "13:00",
                "poi_meal_quality",
                "transit",
                60,
                json.dumps(
                    {
                        "intentType": "meal",
                        "routeAnchor": True,
                        "groundingStatus": "agent_selected_candidate",
                        "required": True,
                    }
                ),
                "routeAnchor=true",
                None,
                None,
                None,
            ),
        )
        db.execute(
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
                "route_quality",
                ids["plan_id"],
                "seg_verifier",
                "seg_meal_quality",
                "poi_verifier",
                "poi_meal_quality",
                "amap-route",
                "transit",
                "公交地铁",
                1,
                0,
                "transit",
                25000,
                5400,
                90,
                5,
                "CNY",
                5,
                "medium",
                "amap-route",
                "[]",
                "[]",
                "{}",
                None,
                timestamp,
            ),
        )
        db.commit()
        report = AgentVerifierService(db).verify_route_feasibility(ids["plan_id"])

    quality = next(check for check in report.checks if check["name"] == "route_quality_verifier")
    assert report.passed is True
    assert quality["status"] == "warning"
    assert quality["blockingIssues"] == []
    assert any("是否顺路仅由完整路线矩阵" in warning for warning in report.soft_failures)


def test_agent_write_does_not_reject_long_provider_meal_leg_by_fixed_threshold():
    with open_db() as db:
        ids = seed_plan(db)
        timestamp = now()
        db.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude, photo_url, source, confidence,
                amap_id, type, district, address, source_note, source_url, photos_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "poi_meal_quality",
                ids["plan_id"],
                "样例餐饮地点",
                "北京",
                "food",
                39.5,
                116.8,
                None,
                "amap-place-search",
                0.91,
                "B000MEAL",
                "餐饮服务;中餐厅",
                "示例区",
                "测试路",
                "groundingStatus：agent_selected_candidate",
                None,
                "[]",
            ),
        )
        db.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time, end_time, poi_id,
                transport_mode, estimated_cost, semantic_metadata_json, notes, weather_signal_id, traffic_crowding_signal_id,
                ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "seg_meal_quality",
                ids["plan_id"],
                "day_verifier",
                2,
                "meal",
                "12:00",
                "13:00",
                "poi_meal_quality",
                "transit",
                60,
                json.dumps(
                    {
                        "intentType": "meal",
                        "routeAnchor": True,
                        "groundingStatus": "agent_selected_candidate",
                        "required": True,
                    }
                ),
                "routeAnchor=true",
                None,
                None,
                None,
            ),
        )
        db.execute(
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
                "route_quality_hard",
                ids["plan_id"],
                "seg_verifier",
                "seg_meal_quality",
                "poi_verifier",
                "poi_meal_quality",
                "amap-webservice",
                "transit",
                "公交地铁",
                1,
                0,
                "transit",
                25000,
                5400,
                90,
                5,
                "CNY",
                5,
                "medium",
                "amap-webservice",
                "[]",
                "[]",
                "{}",
                None,
                timestamp,
            ),
        )
        db.commit()
        missing_proof_report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )
        assert missing_proof_report.passed is False
        assert any("provider_route_matrix_proof_missing" in failure for failure in missing_proof_report.hard_failures)

        version_row = db.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (ids["version_id"],),
        ).fetchone()
        snapshot = json.loads(version_row["snapshot_json"])
        leg = {
            "fromSegmentId": "seg_verifier",
            "toSegmentId": "seg_meal_quality",
            "fromAmapId": "B000PALACE",
            "toAmapId": "B000MEAL",
            "provider": "amap-webservice",
            "distanceMeters": 25000,
            "durationSeconds": 5400,
            "walkingDistanceMeters": 0,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
            "queriedAt": timestamp,
        }
        snapshot["routeInsertionProofs"] = [
            {
                "proofType": "adjacent_route_coverage",
                "operation": "final_route_pair_coverage",
                "status": "passed",
                "networkVerified": True,
                "timeWindowFeasible": True,
                "detourLevel": "not_applicable",
                "legs": {"previousToCandidate": leg},
                "fromSegmentId": "seg_verifier",
                "segmentId": "seg_meal_quality",
                "candidateAmapId": "B000MEAL",
                "routeDecisionContract": _route_decision_contract(),
            }
        ]
        snapshot["routeMatrixExpectedPairs"] = [["seg_verifier", "seg_meal_quality"]]
        db.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False), ids["version_id"]),
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    quality = next(check for check in report.checks if check["name"] == "route_quality_verifier")
    assert report.passed is True
    assert quality["status"] == "warning"
    assert quality["blockingIssues"] == []
    assert "meal_route_load_diagnostic" in quality["days"][0]["issues"]
    assert not any("route_quality:" in failure for failure in report.hard_failures)
    assert any("是否顺路仅由完整路线矩阵" in warning for warning in report.soft_failures)


def test_agent_write_rejects_unacceptable_provider_route_matrix_proof():
    with open_db() as db:
        ids = seed_plan(db)
        row = db.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (ids["version_id"],),
        ).fetchone()
        snapshot = json.loads(row["snapshot_json"])
        snapshot["routeInsertionProofs"] = [
            {
                "status": "failed",
                "networkVerified": True,
                "detourLevel": "unacceptable",
                "timeWindowFeasible": True,
                "failureReason": "provider_route_matrix_unacceptable",
            }
        ]
        db.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot), ids["version_id"]),
        )
        db.commit()

        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    quality = next(check for check in report.checks if check["name"] == "route_quality_verifier")
    assert report.passed is False
    assert "provider_route_matrix_unacceptable" in quality["blockingIssues"]
    assert any("route_quality: provider_route_matrix_unacceptable" in failure for failure in report.hard_failures)


@pytest.mark.parametrize("simple_open_authorized", [False, True])
def test_route_quality_pending_meal_respects_execution_route_policy(simple_open_authorized):
    with open_db() as db:
        ids = seed_plan(db)
        timestamp = now()
        db.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude, photo_url, source, confidence,
                amap_id, type, district, address, source_note, source_url, photos_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "poi_pending_meal_route",
                ids["plan_id"],
                "午餐 当地特色美食（待选择顺路餐馆）",
                "北京",
                "food",
                None,
                None,
                None,
                "agent-text-timeline",
                0.35,
                None,
                "餐饮服务",
                "",
                "",
                "pendingMeal=true；groundingStatus：waiting_for_poi_grounding；intentType：meal；routeAnchor=false；needsConcretePoi=true",
                None,
                "[]",
            ),
        )
        db.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time, end_time, poi_id,
                transport_mode, estimated_cost, semantic_metadata_json, notes, weather_signal_id, traffic_crowding_signal_id,
                ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "seg_pending_meal_route",
                ids["plan_id"],
                "day_verifier",
                2,
                "meal",
                "12:00",
                "13:00",
                "poi_pending_meal_route",
                "transit",
                60,
                json.dumps(
                    {
                        "intentType": "meal",
                        "routeAnchor": False,
                        "groundingStatus": "waiting_for_poi_grounding",
                        "required": False,
                    }
                ),
                "pendingMeal=true；groundingStatus：waiting_for_poi_grounding；routeAnchor=false；needsConcretePoi=true",
                None,
                None,
                None,
            ),
        )
        db.execute(
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
                "route_pending_meal",
                ids["plan_id"],
                "seg_verifier",
                "seg_pending_meal_route",
                "poi_verifier",
                "poi_pending_meal_route",
                "amap-route",
                "transit",
                "公交地铁",
                1,
                0,
                "transit",
                3000,
                1200,
                20,
                5,
                "CNY",
                5,
                "low",
                "amap-route",
                "[]",
                "[]",
                "{}",
                None,
                timestamp,
            ),
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"],
            ids["plan_id"],
            ids["version_id"],
            ids["patch_id"],
            planning_context={"simpleOpenRoutePolicyAuthorized": True} if simple_open_authorized else {},
        )

    quality = next(check for check in report.checks if check["name"] == "route_quality_verifier")
    assert "pending_meal_in_route_options" in quality["blockingIssues"]
    if simple_open_authorized:
        assert report.passed is True
        assert not any("route_quality: pending_meal_in_route_options" in failure for failure in report.hard_failures)
        assert any("pending_meal_in_route_options" in warning for warning in report.soft_failures)
    else:
        assert report.passed is False
        assert any("route_quality: pending_meal_in_route_options" in failure for failure in report.hard_failures)


def test_route_quality_warns_when_routeable_meal_pair_is_missing():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude, photo_url, source, confidence,
                amap_id, type, district, address, source_note, source_url, photos_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "poi_meal_missing_route",
                ids["plan_id"],
                "样例餐饮地点",
                "北京",
                "food",
                39.92,
                116.40,
                None,
                "amap-place-search",
                0.91,
                "B000MEAL2",
                "餐饮服务;中餐厅",
                "示例区",
                "测试路",
                "groundingStatus：agent_selected_candidate",
                None,
                "[]",
            ),
        )
        db.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time, end_time, poi_id,
                transport_mode, estimated_cost, semantic_metadata_json, notes, weather_signal_id, traffic_crowding_signal_id,
                ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "seg_meal_missing_route",
                ids["plan_id"],
                "day_verifier",
                2,
                "meal",
                "12:00",
                "13:00",
                "poi_meal_missing_route",
                "transit",
                60,
                json.dumps(
                    {
                        "intentType": "meal",
                        "routeAnchor": True,
                        "groundingStatus": "agent_selected_candidate",
                        "required": True,
                    }
                ),
                "routeAnchor=true",
                None,
                None,
                None,
            ),
        )
        db.commit()
        report = AgentVerifierService(db).verify_route_feasibility(ids["plan_id"])
        portfolio_report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"],
            ids["plan_id"],
            ids["version_id"],
            ids["patch_id"],
            planning_context={"portfolioCommit": True},
        )

    quality = next(check for check in report.checks if check["name"] == "route_quality_verifier")
    assert quality["status"] == "poor"
    assert "route_coverage_missing" in quality["blockingIssues"]
    assert report.passed is False
    assert any("路线待补全" in warning and "样例餐饮地点" in warning for warning in report.soft_failures)
    assert portfolio_report.passed is False
    assert any("route_quality: route_coverage_missing" in failure for failure in portfolio_report.hard_failures)


def test_verifier_rejects_pending_poi_that_enters_final_itinerary():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cand_pending_verifier",
                ids["session_id"],
                None,
                "故宫",
                "北京",
                "scenic",
                "pending",
                json.dumps([{"id": "B000PALACE", "name": "故宫博物院"}], ensure_ascii=False),
                None,
                now(),
            ),
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    assert report.passed is False
    assert any("pending POI candidate" in failure for failure in report.hard_failures)


def test_verifier_ignores_pending_portfolio_candidate_from_a_different_pool():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            "UPDATE itinerary_segments SET semantic_metadata_json = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "creativeBriefId": "brief_selected",
                        "poolId": "pool_selected",
                        "goalId": "goal_landmark",
                        "planningSlotId": "slot_selected",
                        "intentType": "landmark",
                        "routeAnchor": True,
                        "groundingStatus": "agent_selected_candidate",
                        "required": True,
                    }
                ),
                "seg_verifier",
            ),
        )
        db.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'pending', ?, NULL, ?)
            """,
            (
                "cand_other_portfolio_pool",
                ids["session_id"],
                None,
                "故宫",
                "北京",
                "scenic",
                json.dumps(
                    [
                        {
                            "id": "B000PALACE",
                            "name": "故宫博物院",
                            "briefId": "brief_other",
                            "poolId": "pool_other",
                            "sourceGoalId": "goal_landmark",
                            "planningSlotId": "slot_other",
                        }
                    ],
                    ensure_ascii=False,
                ),
                now(),
            ),
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    pending_check = next(check for check in report.checks if check["name"] == "pending_poi_not_final")
    assert report.passed is True
    assert pending_check["ignoredScopedCandidates"] == ["cand_other_portfolio_pool"]


def test_verifier_rejects_pending_portfolio_candidate_with_only_shared_goal_scope():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute(
            "UPDATE itinerary_segments SET semantic_metadata_json = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "creativeBriefId": "brief_selected",
                        "poolId": "pool_selected",
                        "goalId": "goal_landmark",
                        "planningSlotId": "slot_selected",
                    }
                ),
                "seg_verifier",
            ),
        )
        db.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'pending', ?, NULL, ?)
            """,
            (
                "cand_shared_goal_only",
                ids["session_id"],
                None,
                "故宫",
                "北京",
                "scenic",
                json.dumps(
                    [
                        {
                            "id": "B000PALACE",
                            "name": "故宫博物院",
                            "sourceGoalId": "goal_landmark",
                        }
                    ],
                    ensure_ascii=False,
                ),
                now(),
            ),
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    assert report.passed is False
    assert any("cand_shared_goal_only" in failure for failure in report.hard_failures)


def test_verifier_rejects_missing_patch_or_version_rows():
    with open_db() as db:
        ids = seed_plan(db)
        db.execute("DELETE FROM itinerary_versions WHERE id = ?", (ids["version_id"],))
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    assert report.passed is False
    assert "itinerary_versions row not found" in report.hard_failures


def test_verifier_detects_stale_or_invalid_patch_active_version_changes():
    with open_db() as db:
        ids = seed_plan(db)
        report = AgentVerifierService(db).verify_active_version_unchanged(
            ids["session_id"], ids["version_id"], "invalid_patch"
        )
        assert report.passed is True

        db.execute(
            "UPDATE conversation_sessions SET active_version_id = ? WHERE id = ?", ("ver_changed", ids["session_id"])
        )
        db.commit()
        changed = AgentVerifierService(db).verify_active_version_unchanged(
            ids["session_id"], ids["version_id"], "stale_baseVersion"
        )

    assert changed.passed is False
    assert any("activeVersionId changed" in failure for failure in changed.hard_failures)


def test_verifier_rejects_ticket_fallback_that_masquerades_as_official():
    with open_db() as db:
        ids = seed_plan(db, include_ticket=True)
        db.execute(
            """
            UPDATE ticket_lookup_results
            SET fallback_used = 1, source_name = '官方预约入口', credibility_rank = 'official', caveat = ''
            WHERE id = 'ticket_verifier'
            """
        )
        db.commit()
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    assert report.passed is False
    assert any("masquerades" in failure for failure in report.hard_failures)


def test_verifier_rejects_route_proof_with_tampered_contract_fingerprint():
    contract = _route_decision_contract()
    contract["fingerprint"] = "0" * 64
    verifier = AgentVerifierService(open_db())

    issue = verifier._route_matrix_proof_issue(
        {
            "status": "passed",
            "networkVerified": True,
            "timeWindowFeasible": True,
            "detourLevel": "low",
            "routeDecisionContract": contract,
        },
        [],
    )

    assert issue == "provider_route_decision_contract_invalid"


def test_verifier_get_itinerary_read_only_counter():
    verifier = AgentVerifierService(open_db())

    report = verifier.verify_get_itinerary_read_only(
        {"planning_runs": 1, "ticket_lookup_results": 2}, {"planning_runs": 1, "ticket_lookup_results": 3}
    )

    assert report.passed is False
    assert report.hard_failures == ["GET itinerary changed ticket_lookup_results: 2 -> 3"]


def seed_plan(
    db: sqlite3.Connection,
    poi_source: str = "amap-place-search",
    amap_id: Optional[str] = "B000PALACE",
    confidence: float = 0.91,
    include_ticket: bool = False,
) -> dict[str, str]:
    session_id = "sess_verifier"
    plan_id = "plan_verifier"
    day_id = "day_verifier"
    poi_id = "poi_verifier"
    segment_id = "seg_verifier"
    version_id = "ver_verifier"
    patch_id = "patch_verifier"
    timestamp = now()
    db.executescript(
        """
        DELETE FROM amap_poi_candidates;
        DELETE FROM ticket_lookup_results;
        DELETE FROM itinerary_patches;
        DELETE FROM itinerary_versions;
        DELETE FROM conversation_turns;
        DELETE FROM conversation_sessions;
        DELETE FROM itinerary_segments;
        DELETE FROM itinerary_days;
        DELETE FROM itinerary_plans;
        DELETE FROM pois;
        """
    )
    db.execute(
        """
        INSERT INTO itinerary_plans (
            id, user_id, inspiration_set_id, title, city, template_type,
            budget_target, budget_estimate, budget_delta_explanation,
            decision_rationale, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id,
            "local-user",
            "agent_verifier_eval",
            "Verifier",
            "北京",
            "agent_mvp",
            None,
            0,
            "",
            "",
            "draft",
            timestamp,
            timestamp,
        ),
    )
    db.execute(
        """
        INSERT INTO conversation_sessions (
            id, user_id, title, city, active_plan_id, active_version_id, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, "local-user", "Verifier", "北京", plan_id, version_id, "active", timestamp, timestamp),
    )
    db.execute(
        """
        INSERT INTO itinerary_days (
            id, plan_id, day_number, date, title, weather_summary, risk_summary, total_estimated_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (day_id, plan_id, 1, None, "Day 1", "", "", 0),
    )
    db.execute(
        """
        INSERT INTO pois (
            id, plan_id, name, city, category, latitude, longitude, photo_url, source, confidence,
            amap_id, type, district, address, source_note, source_url, photos_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            poi_id,
            plan_id,
            "故宫博物院",
            "北京",
            "scenic",
            39.918058,
            116.397026,
            None,
            poi_source,
            confidence,
            amap_id,
            "风景名胜",
            "东城区",
            "景山前街4号",
            "高德 POI",
            None,
            "[]",
        ),
    )
    db.execute(
        """
        INSERT INTO itinerary_segments (
            id, plan_id, day_id, segment_order, kind, start_time, end_time, poi_id,
            transport_mode, estimated_cost, semantic_metadata_json, notes, weather_signal_id, traffic_crowding_signal_id,
            ticket_lookup_result_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            segment_id,
            plan_id,
            day_id,
            1,
            "activity",
            "09:00",
            "11:00",
            poi_id,
            "walk",
            0,
            json.dumps(
                {
                    "intentType": "landmark",
                    "routeAnchor": True,
                    "groundingStatus": "agent_selected_candidate",
                    "required": True,
                }
            ),
            "",
            None,
            None,
            "ticket_verifier" if include_ticket else None,
        ),
    )
    if include_ticket:
        db.execute(
            """
            INSERT INTO ticket_lookup_results (
                id, segment_id, ticket_type, status, price_estimate, booking_url, source_name,
                source_url, credibility_rank, queried_at, caveat, provider_name,
                fallback_used, provider_failure_reason, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ticket_verifier",
                segment_id,
                "reservation",
                "unknown",
                0,
                "",
                "暂无可信实时来源",
                "",
                "unavailable",
                timestamp,
                "暂无可信实时来源，请以官方渠道确认为准。",
                "test-provider",
                1,
                "fallback unavailable",
                0,
            ),
        )
    snapshot = {
        "id": plan_id,
        "title": "Verifier",
        "city": "北京",
        "templateType": "agent_mvp",
        "budgetTarget": None,
        "budgetEstimate": 0,
        "budgetDeltaExplanation": "",
        "decisionRationale": "",
        "status": "draft",
        "days": [
            {
                "id": day_id,
                "dayNumber": 1,
                "date": None,
                "title": "Day 1",
                "weatherSummary": "",
                "riskSummary": "",
                "totalEstimatedCost": 0,
                "segments": [
                    {
                        "id": segment_id,
                        "startTime": "09:00",
                        "endTime": "11:00",
                        "kind": "activity",
                        "poi": {
                            "id": poi_id,
                            "name": "故宫博物院",
                            "city": "北京",
                            "category": "scenic",
                            "latitude": 39.918058,
                            "longitude": 116.397026,
                            "photoUrl": None,
                            "source": poi_source,
                            "confidence": confidence,
                            "amapId": amap_id,
                            "type": "风景名胜",
                            "district": "东城区",
                            "address": "景山前街4号",
                            "sourceNote": "高德 POI",
                            "sourceUrl": None,
                            "photos": [],
                        },
                        "transportMode": "walk",
                        "estimatedCost": 0,
                        "notes": "",
                    }
                ],
            }
        ],
        "routeOptions": [],
        "weatherSignals": [],
        "trafficCrowdingSignals": [],
        "ticketLookupResults": [
            {
                "id": "ticket_verifier",
                "segmentId": segment_id,
                "ticketType": "reservation",
                "status": "unknown",
                "priceEstimate": 0,
                "bookingUrl": "",
                "sourceName": "暂无可信实时来源",
                "sourceUrl": "",
                "credibilityRank": "unavailable",
                "queriedAt": timestamp,
                "caveat": "暂无可信实时来源，请以官方渠道确认为准。",
                "providerName": "test-provider",
                "fallbackUsed": True,
                "providerFailureReason": "fallback unavailable",
                "confidence": 0,
            }
        ]
        if include_ticket
        else [],
    }
    db.execute(
        """
        INSERT INTO itinerary_versions (
            id, session_id, plan_id, version_number, source_type, source_turn_id,
            source_patch_id, snapshot_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            session_id,
            plan_id,
            1,
            "agent",
            None,
            patch_id,
            json.dumps(snapshot, ensure_ascii=False),
            timestamp,
        ),
    )
    db.execute(
        """
        INSERT INTO itinerary_patches (
            id, session_id, plan_id, base_version_id, result_version_id, source_type,
            source_turn_id, planning_run_id, operations_json, validation_status,
            validation_errors_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (patch_id, session_id, plan_id, None, version_id, "agent", None, None, "[]", "accepted", "[]", timestamp),
    )
    db.commit()
    return {"session_id": session_id, "plan_id": plan_id, "version_id": version_id, "patch_id": patch_id}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_basic_verifier_reports_quality_gaps_as_soft_failures():
    with open_db() as db:
        ids = seed_plan(db)
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    assert report.passed is True
    assert any("weak day theme" in failure for failure in report.soft_failures)
    assert any("too thin" in failure for failure in report.soft_failures)
    assert any("missing practical notes" in failure for failure in report.soft_failures)


def test_basic_verifier_does_not_report_missing_practical_notes_when_structured_ticket_exists():
    with open_db() as db:
        ids = seed_plan(db, include_ticket=True)
        report = AgentVerifierService(db).verify_agent_write(
            ids["session_id"], ids["plan_id"], ids["version_id"], ids["patch_id"]
        )

    assert report.passed is True
    assert not any("missing practical notes" in failure for failure in report.soft_failures)
