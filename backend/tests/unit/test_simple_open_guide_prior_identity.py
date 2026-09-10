from __future__ import annotations

import copy
import json

import pytest

from backend.tests.unit.test_agent_service import clear_database, open_db
from backend.tests.unit.test_guide_grounded_continuation import _guide_requirement
from backend.tests.unit.test_simple_direction_activation_integrity import _confirmation_ready_snapshot
from backend.tests.unit.test_simple_direction_frontier_service import outcome_for
from src.models.poi import POI
from src.services.conversation_service import ConversationService
from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService
from src.services.simple_direction_frontier_service import SimpleDirectionFrontierService
from src.services.simple_open_direction_service import SimpleOpenDirectionService
from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor


_ROOT = "guide-prior-identity-root"
_REQUEST = "r" * 64


def _refresh_routes(snapshot: dict) -> None:
    expected = []
    verified = []
    for day in snapshot["days"]:
        for left, right in zip(day["segments"], day["segments"][1:]):
            pair = {"fromAmapId": left["poi"]["amapId"], "toAmapId": right["poi"]["amapId"]}
            expected.append(pair)
            proof = {
                **pair,
                "transportMode": "transit",
                "durationSeconds": 900,
                "distanceMeters": 2000,
                "provider": "amap-webservice",
                "queriedAt": "2026-09-05T00:00:00+00:00",
            }
            proof["providerEvidenceFingerprint"] = SimpleOpenDirectionService._provider_evidence_fingerprint(proof)
            verified.append(proof)
    snapshot["simpleOpenRouteAssignment"].update(expectedPairs=expected, verifiedPairs=verified)


def _snapshot(plan_id: str, *, variant: int) -> dict:
    snapshot = _confirmation_ready_snapshot(plan_id)
    for day in snapshot["days"]:
        number = day["dayNumber"]
        campus, meal, park = day["segments"]
        for position, segment in enumerate((campus, meal), start=1):
            segment["poi"].update(
                amapId=f"B{variant}{number}{position}000001",
                name=f"第{variant}{number}高校" if position == 1 else f"第{variant}{number}餐厅",
                latitude=39.8 + variant / 100 + number / 1000,
                longitude=116.3 + variant / 100 + number / 1000,
                address=f"北京市{variant}{number}路{position}号",
            )
            segment["semanticMetadata"].setdefault("scheduleConstraints", {})["replaceablePoi"] = True
        meal_evidence = meal["semanticMetadata"]["scheduleConstraints"]["mealSemanticEvidence"]
        meal_evidence.update(
            amapPoiId=meal["poi"]["amapId"],
            canonicalBrand=f"brand-{variant}-{number}",
            groundedFamilyKey=f"family-{variant}-{number}",
        )
        park["kind"] = "park"
        park["poi"].update(
            amapId=f"BGUIDEPARK{number}",
            name="北海公园" if number == 1 else "景山公园",
            category="park",
            type="风景名胜;公园广场;公园",
            providerType="风景名胜;公园广场;公园",
            providerTypeCode="110101",
        )
        park["semanticMetadata"].update(intentType="park", required=True, requirementLevel="required")
        park["semanticMetadata"].setdefault("scheduleConstraints", {})["replaceablePoi"] = False
    _refresh_routes(snapshot)
    assert SimpleOpenDirectionService._proposal_verifier(snapshot)["confirmationPassed"] is True
    return snapshot


def _bind_guide(snapshot: dict, *, portfolio_id: str) -> None:
    park = snapshot["days"][0]["segments"][2]
    requirement = _guide_requirement(evidence_fingerprint="e" * 64)
    requirement.update(planningSelectionRootTurnId=_ROOT, rootPortfolioId=portfolio_id)
    requirement["requirementFingerprint"] = GuideContinuationRequirementService.requirement_fingerprint(requirement)
    snapshot["guideContinuationRequirement"] = requirement
    park["semanticMetadata"]["scheduleConstraints"].update(
        SimpleOpenItineraryExecutor._guide_evidence_schedule_constraints(
            requirement["placeHints"][0],
            selected=POI(
                **{
                    key: park["poi"][key]
                    for key in ("id", "name", "city", "category", "latitude", "longitude", "source")
                },
                amap_id=park["poi"]["amapId"],
                type=park["poi"]["type"],
            ),
            day_number=1,
            planning_slot_id=park["semanticMetadata"]["planningSlotId"],
            primary_result_count=2,
            guide_match_count=1,
            semantic_rejection=False,
            # This may describe another candidate, so it cannot reject a
            # genuinely new selected guide POI by itself.
            duplicate_rejection=True,
            provider_called=True,
            provider_outcome="success",
            query_text="北海公园",
            search_scope="nearby_low_detour",
            nearby_radius=5000,
        )
    )


def _offer(service, session_id: str, snapshot: dict, *, turn: str, root_id: str = _ROOT, **kwargs) -> dict:
    return service.offer_direction(
        session_id=session_id,
        planning_root_id=root_id,
        source_user_turn_id=turn,
        source_assistant_turn_id=f"assistant-{turn}",
        expected_base_version_id=None,
        source_observation_fingerprint="o" * 64,
        request_contract_fingerprint=_REQUEST,
        snapshot=snapshot,
        request_contract={"routeDecisionContract": snapshot["routeDecisionContract"]},
        **kwargs,
    )


def _claim_new_campuses(service, *, portfolio_id: str, candidate: dict) -> tuple[dict, list[dict]]:
    campuses = [day["segments"][0] for day in candidate["days"]]
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id=_ROOT,
        request_contract_fingerprint=_REQUEST,
        evidence={
            "schemaVersion": "entity-qualification-evidence-v1",
            "contentSha256": "c" * 64,
            "qualificationScheme": "fixture",
            "qualificationValue": "fixture",
            "entities": [{"canonicalName": item["poi"]["name"], "locality": "北京"} for item in campuses],
        },
        locality="北京",
        max_pages_per_query=1,
    )
    service.store.initialize_simple_direction_frontier(
        portfolio_id=portfolio_id,
        frontier=frontier,
        expected_request_contract_fingerprint=_REQUEST,
    )
    attempt = service.claim_frontier_assignment(
        portfolio_id=portfolio_id,
        execution_id="guide-prior-campus-attempt",
        campus_slots=[
            {"dayNumber": number, "slotId": item["semanticMetadata"]["planningSlotId"]}
            for number, item in enumerate(campuses, start=1)
        ],
        request_contract_fingerprint=_REQUEST,
    )
    outcomes = [
        outcome_for(assignment, provider_outcome="success", selected_amap_id=campus["poi"]["amapId"])
        for assignment, campus in zip(attempt["campusAssignments"], campuses)
    ]
    return attempt, outcomes


def _counts(connection) -> dict:
    return {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "agent_plan_proposals",
            "itinerary_versions",
            "itinerary_patches",
            "route_options",
            "timeline_mutation_transactions",
        )
    }


@pytest.mark.parametrize(
    "identity_mode",
    [
        "exact",
        "parent",
        "indoor_parent",
        "physical",
        "prior_parent_current_exact",
        "prior_exact_current_parent",
        "both_parent_fields",
        "older_visible",
        "new",
        "new_other_root",
    ],
)
def test_offer_rejects_old_guide_identity_even_when_v3_global_novelty_passes(identity_mode):
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "guide prior identity")
        service = SimpleOpenDirectionService(connection)
        prior = _snapshot(session.active_plan_id, variant=1)
        candidate = _snapshot(session.active_plan_id, variant=2)
        old_poi = prior["days"][0]["segments"][2]["poi"]
        new_poi = candidate["days"][0]["segments"][2]["poi"]
        if identity_mode not in {"exact", "older_visible"}:
            new_poi.update(amapId="BGUIDENEW01", latitude=39.6, longitude=116.6, address="北京市新址路1号")
        if identity_mode == "parent":
            old_poi["parentPoiId"] = new_poi["parentPoiId"] = "BGUIDESHARED"
        elif identity_mode == "indoor_parent":
            old_poi["indoorParentPoiId"] = new_poi["indoorParentPoiId"] = "BGUIDESHARED"
        elif identity_mode == "physical":
            new_poi.update({key: old_poi[key] for key in ("name", "address", "latitude", "longitude")})
        elif identity_mode == "prior_parent_current_exact":
            old_poi["parentPoiId"] = new_poi["amapId"]
        elif identity_mode == "prior_exact_current_parent":
            new_poi["parentPoiId"] = old_poi["amapId"]
        elif identity_mode == "both_parent_fields":
            old_poi.update(parentPoiId="BGUIDEOLDPARENT", indoorParentPoiId="BGUIDESHARED")
            new_poi.update(parentPoiId="BGUIDENEWPARENT", indoorParentPoiId="BGUIDESHARED")
        _refresh_routes(prior)
        _refresh_routes(candidate)
        first = _offer(service, session.session_id, prior, turn="prior")
        assert first["proposalDelta"] == 1
        portfolio_id = first["rootPortfolioId"]
        if identity_mode == "older_visible":
            latest_prior = _snapshot(session.active_plan_id, variant=3)
            latest_prior["days"][0]["segments"][2]["poi"].update(
                amapId="BLATESTPARK1", name="最新方向公园", latitude=39.5, longitude=116.5, address="北京市最新路1号"
            )
            _refresh_routes(latest_prior)
            assert _offer(service, session.session_id, latest_prior, turn="latest-prior")["proposalDelta"] == 1
        if identity_mode == "new_other_root":
            assert (
                _offer(service, session.session_id, candidate, turn="foreign", root_id="foreign-root")["proposalDelta"]
                == 1
            )
        _bind_guide(candidate, portfolio_id=portfolio_id)
        # Controller-like fields cannot erase the server's actual history or
        # invent exclusion aliases for a legitimately new selected guide POI.
        candidate["priorDirectionPhysicalAliases"] = (
            [] if not identity_mode.startswith("new") else [f"amap:{new_poi['amapId']}"]
        )
        attempt, outcomes = _claim_new_campuses(service, portfolio_id=portfolio_id, candidate=candidate)
        root = service.root_for_planning_root(session_id=session.session_id, planning_root_id=_ROOT)
        visible_ids = list(root["visibleProposalIds"])
        probe = copy.deepcopy(candidate)
        evaluation = service._matching_physical_direction(
            portfolio_id=portfolio_id,
            visible_proposal_ids=visible_ids,
            candidate_snapshot=probe,
            frontier=root["simpleDirectionFrontier"],
            frontier_attempt={**attempt, "validatedOutcomes": outcomes},
            frontier_outcomes=outcomes,
            candidate_confirmation_passed=True,
        )
        assert evaluation.status == "distinct", probe.get("simpleDirectionNoveltyEvidence")
        assert probe["simpleDirectionNoveltyEvidence"]["readyNoveltyPassed"] is True
        before = _counts(connection)
        prior_json = connection.execute(
            "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?", (visible_ids[0],)
        ).fetchone()[0]
        result = _offer(
            service,
            session.session_id,
            candidate,
            turn="candidate",
            frontier_execution_id=attempt["executionId"],
            frontier_outcomes=outcomes,
        )
        after = _counts(connection)
        assert (
            connection.execute(
                "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?", (visible_ids[0],)
            ).fetchone()[0]
            == prior_json
        )
        if identity_mode.startswith("new"):
            assert result["proposalDelta"] == 1, result
            assert result["guideEvidenceUsage"]["status"] == "satisfied"
            assert after["agent_plan_proposals"] == before["agent_plan_proposals"] + 1
            stored = connection.execute(
                "SELECT snapshot_json FROM agent_plan_proposals WHERE portfolio_id = ? AND id != ?",
                (portfolio_id, visible_ids[0]),
            ).fetchone()[0]
            # Readback/adoption verification must not compare a proposal with
            # itself now that it is one of the visible proposals.
            assert service._proposal_verifier(json.loads(stored))["confirmationPassed"] is True
            readback = service.response_material(
                portfolio_id=portfolio_id,
                source_assistant_turn_id="assistant-candidate",
                expected_base_version_id=None,
                read_only=True,
            )
            guide_projection = next(
                item for item in readback["comparisonProjections"] if item.get("guideEvidenceUsage")
            )
            assert guide_projection["confirmationPassed"] is True
            assert guide_projection["guideEvidenceUsage"]["status"] == "satisfied"
            assert guide_projection["guideEvidenceUsage"]["usedPlaces"][0]["amapPoiId"] == new_poi["amapId"]
        else:
            assert result["proposalDelta"] == 0
            assert result["reasonCode"] == "guide_grounded_requirement_unsatisfied", result
            assert result["guideEvidenceUsage"]["status"] == "unsatisfied"
            assert result["guideEvidenceUsage"]["usedPlaces"] == []
            assert result["guideEvidenceUsage"]["rejectionCounts"]["already_used"] == 1
            assert result["guideEvidenceUsage"]["attemptDetails"][0]["reasonCode"] == "already_used"
            assert after == before
        assert {key: after[key] for key in after if key != "agent_plan_proposals"} == {
            key: before[key] for key in before if key != "agent_plan_proposals"
        }


def test_offer_readback_keeps_old_guide_rejected_when_another_new_guide_satisfies_minimum():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "mixed guide prior identity")
        service = SimpleOpenDirectionService(connection)
        prior = _snapshot(session.active_plan_id, variant=1)
        first = _offer(service, session.session_id, prior, turn="prior")
        portfolio_id = first["rootPortfolioId"]
        candidate = _snapshot(session.active_plan_id, variant=2)
        _bind_guide(candidate, portfolio_id=portfolio_id)
        old_constraints = candidate["days"][0]["segments"][2]["semanticMetadata"]["scheduleConstraints"]
        new_park = candidate["days"][1]["segments"][2]
        new_park["poi"].update(amapId="BMIXEDNEWPARK", latitude=39.6, longitude=116.6, address="北京市新园路1号")
        new_constraints = new_park["semanticMetadata"]["scheduleConstraints"]
        requirement = candidate["guideContinuationRequirement"]
        new_hint = copy.deepcopy(requirement["placeHints"][0])
        new_hint["mentionText"] = "景山公园"
        requirement["placeHints"].append(new_hint)
        requirement["requirementFingerprint"] = GuideContinuationRequirementService.requirement_fingerprint(requirement)
        for key in ("guideEvidence", "guideEvidenceAttempt"):
            new_constraints[key] = copy.deepcopy(old_constraints[key])
            new_constraints[key].update(
                mentionText="景山公园",
                dayNumber=2,
                planningSlotId=new_park["semanticMetadata"]["planningSlotId"],
            )
        new_constraints["guideEvidence"]["amapPoiId"] = new_park["poi"]["amapId"]
        new_constraints["guideEvidenceAttempt"]["queryText"] = "景山公园"
        _refresh_routes(candidate)
        original = copy.deepcopy(candidate)
        before = _counts(connection)
        result = _offer(service, session.session_id, candidate, turn="mixed")
        assert result["proposalDelta"] == 1, result
        assert [item["amapPoiId"] for item in result["guideEvidenceUsage"]["usedPlaces"]] == [new_park["poi"]["amapId"]]
        readback = service.response_material(
            portfolio_id=portfolio_id,
            source_assistant_turn_id="assistant-mixed",
            expected_base_version_id=None,
            read_only=True,
        )
        projection = next(item for item in readback["comparisonProjections"] if item.get("guideEvidenceUsage"))
        assert [item["amapPoiId"] for item in projection["guideEvidenceUsage"]["usedPlaces"]] == [
            new_park["poi"]["amapId"]
        ]
        assert projection["guideEvidenceUsage"]["rejectionCounts"]["already_used"] == 1
        stored = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM agent_plan_proposals WHERE portfolio_id = ? ORDER BY created_at DESC LIMIT 1",
                (portfolio_id,),
            ).fetchone()[0]
        )
        stored_old = stored["days"][0]["segments"][2]["semanticMetadata"]["scheduleConstraints"]
        assert "guideEvidence" not in stored_old
        assert stored_old["guideEvidenceAttempt"]["status"] == "rejected"
        assert stored_old["guideEvidenceAttempt"]["reasonCode"] == "already_used"
        assert stored["guideContinuationRequirement"] == requirement
        assert candidate == original
        after = _counts(connection)
        assert after["agent_plan_proposals"] == before["agent_plan_proposals"] + 1
        assert {key: after[key] for key in after if key != "agent_plan_proposals"} == {
            key: before[key] for key in before if key != "agent_plan_proposals"
        }
