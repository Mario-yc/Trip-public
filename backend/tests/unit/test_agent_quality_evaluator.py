from src.runtime.agent_quality_eval import AgentQualityEvaluator


def test_quality_evaluator_reports_safe_fallback_confirmation_lifecycle_metrics():
    evaluator = AgentQualityEvaluator(runtime=None)
    snapshot = {
        "itinerary_versions": [{"id": "ver_unrelated"}, {"id": "ver_1"}],
        "agent_choice_executions": [
            {
                "session_id": "sess_1",
                "source_turn_id": "turn_offer",
                "choice_id": "fallback:confirm_rule_safe_draft:nonce",
                "action": "confirm_rule_safe_draft",
                "status": "succeeded",
                "result_version_id": "ver_1",
            }
        ],
        "conversation_turns": [
            {
                "agent_request_json": {
                    "syntheticAgentChoice": True,
                    "selectedAgentChoice": {"action": "confirm_rule_safe_draft"},
                },
                "agent_response_json": {
                    "mode": "clarification",
                    "choiceOptions": [{"id": "fallback:confirm_rule_safe_draft:nonce"}],
                    "planningSteps": [{"type": "fallback_confirmation_offered"}],
                },
            },
            {
                "agent_request_json": {
                    "syntheticAgentChoice": True,
                    "selectedAgentChoice": {"action": "confirm_rule_safe_draft"},
                },
                "agent_response_json": {
                    "planningSteps": [
                        {
                            "type": "initial_day_slot_provider",
                            "metadata": {"resultPreview": {"executionMode": "user_confirmed_safe_fallback"}},
                        }
                    ]
                },
            },
        ],
    }

    metrics = evaluator._fallback_confirmation_metrics(snapshot)

    assert metrics == {
        "ruleSafeDraftConfirmedCount": 1,
        "deterministicSafeDraftExecutionCount": 1,
        "controllerRetryCount": 0,
        "choiceConsumedCount": 1,
        "choiceExpiredCount": 0,
        "duplicateChoiceExecutionCount": 0,
        "fallbackChoiceVersionDelta": 1,
        "syntheticChoicePreferenceExtractionCount": 0,
        "falseTimelineWriteEventCount": 0,
        "repeatedSameClarificationCount": 0,
    }


def test_quality_evaluator_does_not_count_regular_versions_as_fallback_choice_versions():
    metrics = AgentQualityEvaluator(runtime=None)._fallback_confirmation_metrics(
        {
            "itinerary_versions": [{"id": "ver_1"}, {"id": "ver_2"}],
            "agent_choice_executions": [],
            "conversation_turns": [],
        }
    )

    assert metrics["choiceConsumedCount"] == 0
    assert metrics["fallbackChoiceVersionDelta"] == 0


def test_quality_evaluator_gates_full_itinerary_signature_distribution_and_meal_brand_reuse():
    evaluator = AgentQualityEvaluator(runtime=None)
    days = [
        {
            "dayNumber": 1,
            "segments": [
                {"kind": "visit", "poi": {"name": "北京大学"}},
                {"kind": "meal", "poi": {"name": "四季民福"}},
                {"kind": "meal", "poi": {"name": "四季民福"}},
            ],
        }
    ]
    metrics = evaluator._full_itinerary_signature_metrics(days)

    assert metrics["fullItinerarySignature"]
    assert metrics["duplicateMealBrandCount"] == 1

    results = [
        {"metrics": {"fullItinerarySignature": signature}}
        for signature in ["a", "a", "a", "b", "b", "b", "c", "c", "c", "d", "d", "d"]
    ]
    aggregate, failures = evaluator._aggregate_metrics(
        results,
        {
            "minFullItinerarySampleCount": 12,
            "minDistinctFullItinerarySignatures": 4,
            "maxFullItinerarySignatureShare": 0.35,
        },
    )

    assert aggregate["distinctFullItinerarySignatureCount"] == 4
    assert aggregate["maxFullItinerarySignatureShare"] == 0.25
    assert failures == []


def test_quality_evaluator_gates_required_pool_coverage_before_optional_budget_use():
    evaluator = AgentQualityEvaluator(runtime=None)
    artifact = {
        "planningSteps": [
            {
                "metadata": {
                    "resultPreview": {
                        "poolReports": [
                            {
                                "poolId": "campus",
                                "intentType": "campus_visit",
                                "priorityClass": "required_explicit",
                                "coverageStatus": "covered",
                            },
                            {
                                "poolId": "museum",
                                "intentType": "museum",
                                "priorityClass": "required_explicit",
                                "coverageStatus": "covered",
                            },
                            {
                                "poolId": "meal",
                                "intentType": "meal",
                                "priorityClass": "required_functional",
                                "coverageStatus": "covered",
                            },
                            {
                                "poolId": "walk",
                                "intentType": "area_walk",
                                "priorityClass": "optional_creative",
                                "coverageStatus": "unresolved",
                            },
                        ]
                    }
                }
            }
        ],
        "toolEvents": [],
    }

    metrics = evaluator._intent_pool_priority_metrics(artifact)
    failures = evaluator._expectation_failures(
        {**metrics, "verifierPassed": True},
        {
            "requiredIntentPoolCoverage": {"campus_visit": "covered", "museum": "covered", "meal": "covered"},
            "requiredPoolsMustPrecedeOptional": True,
        },
    )

    assert metrics["requiredPoolsPrecedeOptional"] is True
    assert failures == []


def test_quality_evaluator_reports_semantic_integrity_and_read_only_side_effect_proof():
    evaluator = AgentQualityEvaluator(runtime=None)
    artifact = {
        "planningSteps": [
            {
                "type": "agent_decision",
                "metadata": {"source": "controller"},
            },
            {
                "metadata": {
                    "resultPreview": {
                        "poolReports": [
                            {
                                "intentType": "museum",
                                "selectedCanonicalEntities": ["中国美术馆"],
                                "semanticGateResults": [
                                    {"candidateName": "中国美术馆", "passed": True},
                                    {"candidateName": "花海畔溪谷", "passed": False},
                                ],
                            }
                        ]
                    }
                }
            },
        ],
        "toolEvents": [],
        "agentObservations": [
            {
                "requirementCoverage": {
                    "required": [
                        {
                            "intentType": "museum",
                            "invalidClaims": [{"segmentId": "seg_bad", "poiName": "花海畔溪谷"}],
                        }
                    ]
                }
            }
        ],
        "sessionSnapshot": {
            "conversation_turns": [
                {
                    "agent_response_json": {
                        "timelineQueryResult": {"invalidClaims": [{"poiName": "花海畔溪谷"}]},
                        "readOnlySideEffectProof": {
                            "externalCalls": 0,
                            "patchCount": 0,
                            "versionDelta": 0,
                            "unchangedVerifierPassed": True,
                        },
                    }
                }
            ]
        },
        "itinerarySnapshot": {},
        "verifierReport": {},
        "context": {},
    }

    metrics = evaluator._metrics(artifact, {"activeVersionId": "ver_1"})
    failures = evaluator._expectation_failures(
        metrics,
        {
            "museumSemanticMismatchSelectedCount": 0,
            "museumTimelineInvalidClaimCount": 1,
            "readOnlyExternalCallCount": 0,
            "readOnlyPatchCount": 0,
            "readOnlyVersionDelta": 0,
            "readOnlyUnchangedVerifierMustPass": True,
            "genericToolLoopWithoutDecisionCount": 0,
            "goalClaimMismatchCount": 1,
        },
    )

    assert metrics["museumSemanticRejectedCandidateCount"] == 1
    assert metrics["museumSemanticMismatchSelectedCount"] == 0
    assert failures == []


def test_quality_evaluator_exposes_and_checks_agent_decision_contract():
    evaluator = AgentQualityEvaluator(runtime=None)
    artifact = {
        "agentDecision": {
            "primaryAction": "patch_itinerary",
            "requiredTools": ["read_itinerary", "patch_itinerary", "unauthorized_tool"],
            "stopCondition": {"type": "verified_patch"},
        },
        "planningSteps": [
            {
                "type": "agent_decision",
                "metadata": {
                    "primaryAction": "patch_itinerary",
                    "effectiveTools": ["read_itinerary", "patch_itinerary"],
                    "effectiveWriteRisk": "medium",
                    "accepted": True,
                    "source": "controller",
                    "proposedExecutionRoute": "deterministic_timeline_then_bounded_tool_loop",
                    "decisionDurationMs": 37,
                },
            }
        ],
        "toolEvents": [],
        "context": {},
        "itinerarySnapshot": {},
        "verifierReport": {},
        "sessionSnapshot": {},
    }

    metrics = evaluator._metrics(artifact, {"activeVersionId": None})
    failures = evaluator._expectation_failures(
        metrics,
        {
            "agentDecisionEventRequired": True,
            "agentDecisionPrimaryAction": "patch_itinerary",
            "agentDecisionEffectiveWriteRisk": "medium",
            "agentDecisionRequiredToolsExact": ["read_itinerary", "patch_itinerary", "unauthorized_tool"],
            "agentDecisionEffectiveToolsExact": ["read_itinerary", "patch_itinerary"],
            "agentDecisionStopConditionType": "verified_patch",
            "agentDecisionSource": "controller",
            "agentDecisionProposedExecutionRoute": "deterministic_timeline_then_bounded_tool_loop",
            "agentDecisionPolicyAccepted": True,
            "maxAgentDecisionDurationMs": 100,
        },
    )

    assert metrics["agentDecisionDurationMs"] == 37
    assert failures == []


def test_quality_evaluator_does_not_count_bounded_stop_as_executed_action():
    evaluator = AgentQualityEvaluator(runtime=None)
    base = {
        "agentDecisions": [
            {
                "type": "agent_decision",
                "metadata": {"cycleIndex": 2, "primaryAction": "resolve_poi", "accepted": True, "source": "controller"},
            }
        ],
        "planningSteps": [],
        "toolEvents": [],
        "context": {},
        "itinerarySnapshot": {},
        "verifierReport": {},
        "sessionSnapshot": {},
    }
    synthetic = {
        **base,
        "agentActionOutcomes": [
            {
                "type": "agent_action_outcome",
                "metadata": {
                    "resultPreview": {
                        "cycleIndex": 2,
                        "action": "resolve_poi",
                        "executionRoute": "bounded_cycle_stop",
                        "status": "no_change",
                    }
                },
            }
        ],
    }
    real = {
        **base,
        "agentActionOutcomes": [
            {
                "type": "agent_action_outcome",
                "metadata": {
                    "resultPreview": {
                        "cycleIndex": 2,
                        "action": "resolve_poi",
                        "executionRoute": "poi_grounding",
                        "status": "needs_confirmation",
                    }
                },
            }
        ],
    }

    assert evaluator._metrics(synthetic, {})["unexecutedAcceptedContinuingDecisionCount"] == 1
    assert evaluator._metrics(real, {})["unexecutedAcceptedContinuingDecisionCount"] == 0
    failures = evaluator._expectation_failures(
        evaluator._metrics(synthetic, {}),
        {"unexecutedAcceptedContinuingDecisionCount": 0},
    )
    assert failures == ["unexecutedAcceptedContinuingDecisionCount mismatch: expected 0, got 1"]


def test_quality_evaluator_counts_real_deterministic_timeline_patch_as_executed():
    evaluator = AgentQualityEvaluator(runtime=None)
    artifact = {
        "agentDecisions": [
            {
                "type": "agent_decision",
                "metadata": {
                    "cycleIndex": 0,
                    "primaryAction": "patch_itinerary",
                    "accepted": True,
                    "source": "controller",
                },
            }
        ],
        "agentActionOutcomes": [
            {
                "type": "agent_action_outcome",
                "metadata": {
                    "resultPreview": {
                        "cycleIndex": 0,
                        "action": "patch_itinerary",
                        "executionRoute": "deterministic_timeline_patch",
                        "status": "success",
                    }
                },
            }
        ],
        "planningSteps": [],
        "toolEvents": [],
        "context": {},
        "itinerarySnapshot": {},
        "verifierReport": {},
        "sessionSnapshot": {},
    }

    assert evaluator._metrics(artifact, {})["unexecutedAcceptedContinuingDecisionCount"] == 0


def test_quality_evaluator_does_not_treat_proposed_route_as_actual_without_outcome():
    evaluator = AgentQualityEvaluator(runtime=None)
    decision_event = {
        "type": "agent_decision",
        "metadata": {
            "cycleIndex": 0,
            "primaryAction": "draft_itinerary",
            "accepted": True,
            "proposedExecutionRoute": "staged_initial_pipeline",
            "actualExecutionRoute": "staged_initial_pipeline",
        },
    }
    artifact = {
        "agentDecisions": [decision_event],
        "agentActionOutcomes": [],
        "planningSteps": [decision_event],
        "toolEvents": [],
        "context": {},
        "itinerarySnapshot": {},
        "verifierReport": {},
        "sessionSnapshot": {},
    }

    assert evaluator._metrics(artifact, {})["actualExecutionRoute"] is None


def test_quality_evaluator_measures_control_ownership_not_only_event_presence():
    evaluator = AgentQualityEvaluator(runtime=None)
    decision = {
        "type": "agent_decision",
        "metadata": {
            "source": "controller",
            "controlOwner": "model_controller",
            "primaryAction": "read_itinerary",
            "accepted": True,
            "plannerCalled": False,
            "timelineParserCalledBeforeDecision": False,
            "actionDirectiveSource": "model",
            "proposedExecutionRoute": "read_only",
            "actualExecutionRoute": "read_only",
        },
    }
    post_decision = {
        "type": "agent_decision",
        "metadata": {
            **decision["metadata"],
            "primaryAction": "finish",
            "proposedExecutionRoute": "terminal_response",
            "actualExecutionRoute": "terminal_response",
            "postObservationDecision": True,
        },
    }
    artifact = {
        "agentDecision": decision["metadata"],
        "agentDecisions": [decision, post_decision],
        "planningSteps": [decision, post_decision],
        "toolEvents": [],
        "context": {"latestUserMessage": "当前安排是什么"},
        "itinerarySnapshot": {},
        "verifierReport": {},
        "sessionSnapshot": {},
    }

    metrics = evaluator._metrics(artifact, {})

    assert metrics["modelPrimaryDecisionRatio"] == 1.0
    assert metrics["plannerCalledOnHealthyControllerCount"] == 0
    assert metrics["timelineParserPreDecisionCount"] == 0
    assert metrics["deterministicBusinessPreemptionCount"] == 0
    assert metrics["executionRouteMismatchCount"] == 0
    assert metrics["actionDirectiveModelCount"] == 2
    assert metrics["postObservationDecisionCount"] == 1


def test_quality_evaluator_defaults_mock_map_provider_only_for_candidate_first_expectations():
    evaluator = AgentQualityEvaluator(runtime=None)

    assert (
        evaluator._should_default_mock_map_provider(
            {"expectations": {"minRouteableAnchorsPerDay": 2, "riskSearchMustSkipOrdinaryMeals": True}},
            mock_providers=True,
        )
        is True
    )
    assert (
        evaluator._should_default_mock_map_provider(
            {"expectations": {"forbidMockOrSyntheticMealsMapReady": True}},
            mock_providers=True,
        )
        is True
    )
    assert (
        evaluator._should_default_mock_map_provider(
            {"expectations": {"activeVersionEventuallyCreated": True, "secondTurnDoesNotAskForTripDates": True}},
            mock_providers=True,
        )
        is False
    )
    assert (
        evaluator._should_default_mock_map_provider(
            {"expectations": {"minRouteableAnchorsPerDay": 2}, "mockMapProvider": {"recover": True}},
            mock_providers=True,
        )
        is False
    )
    assert (
        evaluator._should_default_mock_map_provider({"expectations": {"minRouteableAnchorsPerDay": 2}}, False) is False
    )


def test_quality_evaluator_treats_ddgs_as_stable_risk_search_provider():
    evaluator = AgentQualityEvaluator(runtime=None)
    artifact = {
        "itinerarySnapshot": {
            "days": [{"dayNumber": 1, "segments": [{"id": "seg_pku", "kind": "visit", "poi": {"name": "北京大学"}}]}],
            "poiRiskAlerts": [
                {
                    "segmentId": "seg_pku",
                    "status": "available",
                    "sources": [
                        {
                            "type": "webSearchProviderDiagnostics",
                            "attemptedProviders": ["ddgs"],
                            "successfulProviders": ["ddgs"],
                            "failedProviders": [],
                            "skippedProviders": [],
                        },
                        {
                            "type": "riskSearchDiagnostics",
                            "query": "北京 北京大学 2026 国庆 官方公告 预约 限流",
                            "queryLength": 31,
                            "acceptedSourceCount": 1,
                        },
                    ],
                }
            ],
        },
        "verifierReport": {},
        "planningSteps": [],
        "toolEvents": [],
        "sessionSnapshot": {},
    }

    metrics = evaluator._metrics(artifact, {"activeVersionId": "ver_1"})
    failures = evaluator._expectation_failures(
        metrics,
        {
            "riskSearchProviderDiagnosticsMustBePresent": True,
            "riskSearchMustAttemptAtLeastOneStableProvider": True,
            "riskSearchAttemptedProvidersMustInclude": ["ddgs"],
            "riskSearchSuccessfulProvidersMustInclude": ["ddgs"],
            "riskSearchAcceptedSourceCountAtLeast": 1,
        },
    )

    assert metrics["riskSearchHasAtLeastOneConfiguredStableProvider"] is True
    assert metrics["webSearchAttemptedProviders"] == ["ddgs"]
    assert metrics["webSearchSuccessfulProviders"] == ["ddgs"]
    assert failures == []


def test_quality_evaluator_flags_food_route_risk_mode_and_night_view_regressions():
    evaluator = AgentQualityEvaluator(runtime=None)
    artifact = {
        "context": {
            "effectiveUserMessage": "国庆两日游，公交地铁优先，想体验当地特色美食",
            "understoodRequirements": {"transportMode": "transit"},
        },
        "itinerarySnapshot": {
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "seg_meal",
                            "kind": "meal",
                            "notes": "requiredGrounding=true; explicit_food_experience",
                            "poi": {
                                "id": "poi_meal",
                                "name": "佟园食堂",
                                "source": "amap-place-search",
                                "amapId": "amap_meal",
                                "longitude": 116.3,
                                "latitude": 39.9,
                                "type": "餐饮服务;中餐厅",
                                "category": "餐饮服务",
                                "sourceNote": "intentType：meal；groundingStatus：agent_selected_candidate",
                            },
                        },
                        {
                            "id": "seg_night_1",
                            "kind": "visit",
                            "notes": "intentType：night_view",
                            "poi": {
                                "name": "河岸公共空间甲",
                                "sourceNote": "intentType：night_view",
                                "type": "滨水;公共空间",
                            },
                        },
                    ],
                },
                {
                    "dayNumber": 2,
                    "segments": [
                        {
                            "id": "seg_night_2",
                            "kind": "visit",
                            "notes": "intentType：night_view",
                            "poi": {
                                "name": "河岸公共空间乙",
                                "sourceNote": "intentType：night_view",
                                "type": "滨水;公共空间",
                            },
                        }
                    ],
                },
            ],
            "routeOptions": [
                {
                    "id": "route_1",
                    "isSelected": True,
                    "mode": "bicycling",
                    "transportMode": "bicycling",
                    "providerPayload": {},
                }
            ],
            "poiRiskAlerts": [{"segmentId": "seg_meal", "status": "unavailable"}],
        },
        "verifierReport": {
            "checks": [
                {
                    "name": "route_quality_verifier",
                    "status": "poor",
                    "warnings": ["Day 1 餐饮相邻路线偏绕：19.6 km / 76 min。"],
                    "days": [{"dayNumber": 1, "totalRouteDistanceKm": 47.7, "issues": ["meal_detour_high"]}],
                }
            ]
        },
        "planningSteps": [],
        "toolEvents": [],
        "sessionSnapshot": {},
    }

    metrics = evaluator._metrics(artifact, {"activeVersionId": "ver_1"})
    failures = evaluator._expectation_failures(
        metrics,
        {
            "explicitLocalFoodMustBeRelevant": True,
            "forbidInstitutionalOrHotelMealsForLocalFood": True,
            "forbidPoorRouteQuality": True,
            "forbidMealDetourHigh": True,
            "preferredTransportMode": "transit",
            "maxNonPreferredRouteLegRatio": 0.25,
            "nonPreferredRouteRequiresCaveat": True,
            "riskSearchMustSkipOrdinaryMeals": True,
            "nightViewMustBeDistinctFamily": True,
        },
    )

    assert metrics["explicitLocalFoodGenericCount"] == 1
    assert metrics["institutionalMealCount"] == 1
    assert metrics["mealDetourHighCount"] == 1
    assert metrics["riskMealSearchCount"] == 1
    assert metrics["nonPreferredRouteWithoutCaveatCount"] == 1
    assert metrics["nightViewDuplicateFamilyCount"] == 1
    assert len(failures) >= 7


def test_quality_evaluator_rejects_mock_food_as_map_ready_or_routeable():
    evaluator = AgentQualityEvaluator(runtime=None)
    artifact = {
        "context": {"effectiveUserMessage": "想体验当地特色美食"},
        "itinerarySnapshot": {
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "seg_meal",
                            "kind": "meal",
                            "routeAnchor": True,
                            "notes": "requiredGrounding=true; explicit_food_experience",
                            "poi": {
                                "id": "mock_amap_food_1",
                                "name": "北京本地菜餐厅",
                                "source": "amap-place-search",
                                "amapId": "mock_amap_food_1",
                                "longitude": 116.3,
                                "latitude": 39.9,
                                "type": "餐饮服务;中餐厅",
                                "sourceNote": (
                                    "CLI eval mock AMap candidate; not a real map POI; "
                                    "groundingStatus：agent_selected_candidate"
                                ),
                                "grounding": {"mapReady": True, "routeable": True},
                            },
                        }
                    ],
                }
            ]
        },
        "verifierReport": {},
        "planningSteps": [],
        "toolEvents": [],
        "sessionSnapshot": {},
    }

    metrics = evaluator._metrics(artifact, {"activeVersionId": "ver_1"})
    failures = evaluator._expectation_failures(
        metrics,
        {
            "forbidMockOrSyntheticMealsMapReady": True,
            "forbidMockOrSyntheticMealsRouteable": True,
            "minRouteableAnchorsPerDay": 1,
        },
    )

    assert metrics["mockOrSyntheticMealMapReadyCount"] == 1
    assert metrics["mockOrSyntheticMealRouteableCount"] == 1
    assert metrics["routeableAnchorsByDay"]["1"] == 0
    assert "mock/synthetic meals marked map-ready: 1" in failures
    assert "mock/synthetic meals marked routeable: 1" in failures


def test_quality_evaluator_counts_only_real_night_view_family_duplicates():
    evaluator = AgentQualityEvaluator(runtime=None)
    artifact = {
        "context": {},
        "itinerarySnapshot": {
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "seg_waterfront_a",
                            "kind": "visit",
                            "notes": "intentType：night_view",
                            "poi": {
                                "name": "水岸公共空间甲",
                                "sourceNote": "intentType：night_view",
                                "type": "滨水;公共空间",
                            },
                        },
                        {
                            "id": "seg_waterfront_b",
                            "kind": "visit",
                            "notes": "intentType：night_view",
                            "poi": {
                                "name": "水岸公共空间乙",
                                "sourceNote": "intentType：night_view",
                                "type": "滨水;公共空间",
                            },
                        },
                    ],
                },
                {
                    "dayNumber": 2,
                    "segments": [
                        {
                            "id": "seg_bridge",
                            "kind": "visit",
                            "notes": "先保留待地图候选确认的主题需求；intentType：night_view",
                            "poi": {"name": "桥中", "sourceNote": "intentType：night_view", "type": "地名地址信息"},
                        },
                        {
                            "id": "seg_temple",
                            "kind": "visit",
                            "notes": "先保留待地图候选确认的主题需求；intentType：night_view",
                            "poi": {"name": "大佛寺", "sourceNote": "intentType：night_view", "type": "风景名胜;寺庙"},
                        },
                        {
                            "id": "seg_pending_night_1",
                            "kind": "visit",
                            "notes": "intentType：night_view；groundingStatus：waiting_for_poi_grounding",
                            "poi": {
                                "name": "夜景观景点",
                                "sourceNote": "groundingStatus：waiting_for_poi_grounding",
                                "type": "风景名胜",
                            },
                        },
                        {
                            "id": "seg_pending_night_2",
                            "kind": "visit",
                            "notes": "intentType：night_view；groundingStatus：waiting_for_poi_grounding",
                            "poi": {
                                "name": "夜景观景点",
                                "sourceNote": "groundingStatus：waiting_for_poi_grounding",
                                "type": "风景名胜",
                            },
                        },
                    ],
                },
            ]
        },
        "verifierReport": {},
        "planningSteps": [],
        "toolEvents": [],
        "sessionSnapshot": {},
    }

    metrics = evaluator._metrics(artifact, {"activeVersionId": "ver_1"})

    assert metrics["nightViewDuplicateFamilyCount"] == 1


def test_quality_evaluator_ignores_short_non_preferred_last_mile_in_route_ratio():
    evaluator = AgentQualityEvaluator(runtime=None)
    artifact = {
        "context": {
            "effectiveUserMessage": "公交地铁优先",
            "understoodRequirements": {"transportMode": "transit"},
        },
        "itinerarySnapshot": {
            "days": [],
            "routeOptions": [
                {
                    "id": "route_transit",
                    "isSelected": True,
                    "mode": "transit",
                    "transportMode": "transit",
                    "distanceMeters": 5000,
                    "durationMinutes": 30,
                    "providerPayload": {},
                },
                {
                    "id": "route_short_walk",
                    "isSelected": True,
                    "mode": "walking",
                    "transportMode": "walking",
                    "distanceMeters": 900,
                    "durationMinutes": 12,
                    "providerPayload": {
                        "fallbackFromPreferredMode": True,
                        "userVisibleCaveat": "公交/地铁无可用路线，暂用步行/骑行估算。",
                    },
                },
            ],
        },
        "verifierReport": {},
        "planningSteps": [],
        "toolEvents": [],
        "sessionSnapshot": {},
    }

    metrics = evaluator._metrics(artifact, {"activeVersionId": "ver_1"})

    assert metrics["nonPreferredRouteModeCount"] == 1
    assert metrics["nonPreferredRouteWithoutCaveatCount"] == 0
    assert metrics["nonPreferredRouteLegRatio"] == 0.0


def test_quality_evaluator_checks_timeline_precision_and_meal_cost_markers():
    evaluator = AgentQualityEvaluator(runtime=None)
    artifact = {
        "context": {
            "timelinePrecisionExpectations": {
                "targetDayNumber": 2,
                "unchangedDaySegmentIds": {"1": ["seg_d1_campus", "seg_d1_night"]},
            }
        },
        "itinerarySnapshot": {
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {"id": "seg_d1_campus", "kind": "visit", "startTime": "09:00", "poi": {"name": "北京大学"}},
                        {"id": "seg_d1_night", "kind": "visit", "startTime": "19:30", "poi": {"name": "景山公园夜景"}},
                    ],
                },
                {
                    "dayNumber": 2,
                    "segments": [
                        {"id": "seg_d2_campus", "kind": "visit", "startTime": "15:00", "poi": {"name": "清华大学"}},
                        {
                            "id": "seg_d2_dinner",
                            "kind": "meal",
                            "startTime": "18:00",
                            "estimatedCost": 240,
                            "notes": "costBasis=per_person; costPerPerson=120; partySize=2; totalCost=240; costSource=budget_policy",
                            "poi": {
                                "name": "晚餐 当地特色美食",
                                "sourceNote": "groundingStatus：waiting_for_poi_grounding",
                            },
                        },
                        {
                            "id": "seg_d2_night",
                            "kind": "visit",
                            "startTime": "19:30",
                            "poi": {"name": "奥林匹克塔夜景"},
                        },
                    ],
                },
            ]
        },
        "planningSteps": [
            {
                "type": "timeline_edit",
                "metadata": {
                    "resultPreview": {
                        "timelineCommand": {"operation": "replace_or_fill", "dayNumber": 2},
                        "globalReorder": False,
                    }
                },
            }
        ],
        "toolEvents": [],
        "verifierReport": {},
        "sessionSnapshot": {},
    }

    metrics = evaluator._metrics(artifact, {"activeVersionId": "ver_1"})
    failures = evaluator._expectation_failures(
        metrics,
        {
            "timelinePrecisionEditFillDay2Dinner": True,
            "mealCostPerPersonDisplay": True,
        },
    )

    assert metrics["timelineEditEventPresent"] is True
    assert metrics["targetDayDinnerCount"] == 1
    assert metrics["timelineEditUnchangedDaysMatch"] is True
    assert metrics["mealCostPerPersonMismatchCount"] == 0
    assert failures == []


def test_quality_evaluator_checks_route_optimization_and_schedule_recompute_metrics():
    evaluator = AgentQualityEvaluator(runtime=None)
    artifact = {
        "context": {
            "effectiveUserMessage": "公交地铁优先",
            "understoodRequirements": {"transportMode": "transit"},
        },
        "itinerarySnapshot": {
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {"id": "seg_d1_start", "kind": "visit", "startTime": "10:00", "poi": {"name": "北京大学"}},
                        {"id": "seg_d1_next", "kind": "visit", "startTime": "10:14", "poi": {"name": "清华大学"}},
                    ],
                }
            ],
            "routeOptions": [
                {
                    "id": "route_transit",
                    "isSelected": True,
                    "mode": "transit",
                    "transportMode": "transit",
                    "distanceMeters": 5000,
                    "durationMinutes": 24,
                    "providerPayload": {},
                },
                {
                    "id": "route_short_walk",
                    "isSelected": True,
                    "mode": "walking",
                    "transportMode": "walking",
                    "distanceMeters": 800,
                    "durationMinutes": 8,
                    "providerPayload": {
                        "fallbackFromPreferredMode": "transit",
                        "fallbackReason": "preferred_mode_route_unavailable",
                        "userVisibleCaveat": "公交/地铁无可用路线，暂用步行估算。",
                    },
                },
            ],
        },
        "planningSteps": [
            {
                "type": "route_optimize",
                "metadata": {
                    "resultPreview": {
                        "optimizationObjective": "fastest",
                        "routeOptimization": {"objective": "fastest", "changedCount": 1},
                        "scheduleUpdatedCount": 1,
                    }
                },
            },
            {
                "type": "route_optimize",
                "metadata": {
                    "resultPreview": {
                        "optimizationObjective": "cheapest",
                        "routeOptimization": {"objective": "cheapest", "changedCount": 1},
                        "scheduleUpdatedCount": 2,
                    }
                },
            },
        ],
        "toolEvents": [],
        "verifierReport": {},
        "sessionSnapshot": {},
    }

    metrics = evaluator._metrics(artifact, {"activeVersionId": "ver_1"})
    failures = evaluator._expectation_failures(
        metrics,
        {
            "selectedRouteModesMustInclude": ["transit"],
            "maxNonPreferredRouteLegRatio": 0,
            "nonPreferredRouteRequiresCaveat": True,
            "nonPreferredRouteFallbackReasonsMustInclude": ["preferred_mode_route_unavailable"],
            "routeOptimizationObjectivesMustInclude": ["fastest", "cheapest"],
            "routeOptimizationChangedCountAtLeast": 2,
            "scheduleUpdatedCountAtLeast": 2,
            "segmentStartTimes": {"seg_d1_next": "10:14"},
        },
    )

    assert metrics["selectedRouteModes"] == ["transit", "walking"]
    assert metrics["nonPreferredRouteWithoutCaveatCount"] == 0
    assert metrics["nonPreferredRouteFallbackReasons"] == ["preferred_mode_route_unavailable"]
    assert metrics["routeOptimizationObjectives"] == ["cheapest", "fastest"]
    assert metrics["routeOptimizationChangedCount"] == 2
    assert metrics["scheduleUpdatedCount"] == 2
    assert failures == []


def test_quality_evaluator_derives_daily_occurrence_and_adjacent_preflight_metrics_from_artifact():
    artifact = {
        "context": {},
        "itinerarySnapshot": {
            "portfolioGoalOccurrencePlan": {
                "occurrences": [
                    {
                        "occurrenceId": "occ-campus-day-1",
                        "sourceGoalId": "goal_campus",
                        "intentType": "campus_visit",
                        "dayNumber": 1,
                        "requirementLevel": "hard",
                        "distinctGroupId": "campus",
                    },
                    {
                        "occurrenceId": "occ-meal-day-1",
                        "sourceGoalId": "goal_meal",
                        "intentType": "meal",
                        "dayNumber": 1,
                        "requirementLevel": "explicit_soft",
                    },
                ]
            },
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "poi": {"amapId": "campus-1", "city": "北京", "source": "amap-place-search"},
                            "semanticMetadata": {
                                "occurrenceId": "occ-campus-day-1",
                                "sourceGoalId": "goal_campus",
                                "planningSlotId": "slot-campus",
                            },
                        },
                        {
                            "poi": {"amapId": "meal-1", "city": "北京", "source": "amap-place-search"},
                            "semanticMetadata": {
                                "occurrenceId": "occ-meal-day-1",
                                "sourceGoalId": "goal_meal",
                                "planningSlotId": "slot-meal",
                            },
                        },
                    ],
                }
            ],
        },
        "planningSteps": [
            {
                "metadata": {
                    "resultPreview": {
                        "adjacentSearchCenter": {"longitude": 116.4},
                        "mutationPreflight": {"writeAttemptCount": 0, "versionDelta": 0, "patchDelta": 0},
                        "routeVerification": {"status": "verified"},
                    }
                }
            },
            {
                "metadata": {
                    "resultPreview": {
                        "timelineMutationOutcome": {
                            "selectedAmapPoiId": "museum-1",
                            "versionDelta": 1,
                            "patchDelta": 1,
                            "rollbackCount": 0,
                        }
                    }
                }
            },
        ],
        "toolEvents": [],
    }

    metrics = AgentQualityEvaluator(runtime=None)._daily_goal_convergence_metrics(
        artifact,
        artifact["itinerarySnapshot"],
    )

    assert metrics["goalOccurrenceExpectedCount"] == 2
    assert metrics["goalOccurrenceActualCount"] == 2
    assert metrics["goalOccurrenceCoverageFailureCount"] == 0
    assert metrics["dailyGoalCoverageFailureCount"] == 0
    assert metrics["backendRegexOccurrenceDecisionCount"] == 0
    assert metrics["mutationPreflightWriteAttemptCount"] == 0
    assert metrics["insertionRouteVerifiedCandidateCount"] == 1
    assert metrics["localAddVersionDelta"] == metrics["localAddPatchDelta"] == 1


def test_quality_evaluator_reads_proposal_snapshot_evidence_before_selection():
    proposal_snapshot = {
        "portfolioGoalOccurrencePlan": {
            "occurrences": [
                {
                    "occurrenceId": "occ-campus-day-1",
                    "sourceGoalId": "goal_campus",
                    "intentType": "campus_visit",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                    "source": "controller_day_strategy",
                },
                {
                    "occurrenceId": "occ-meal-day-1",
                    "sourceGoalId": "goal_meal",
                    "intentType": "meal",
                    "dayNumber": 1,
                    "requirementLevel": "explicit_soft",
                    "source": "controller_day_strategy",
                },
            ]
        },
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "poi": {"amapId": "campus-1", "city": "北京", "source": "amap-place-search"},
                        "semanticMetadata": {
                            "occurrenceId": "occ-campus-day-1",
                            "goalId": "goal_campus",
                            "planningSlotId": "slot-campus",
                        },
                    },
                    {
                        "poi": {"amapId": "meal-1", "city": "北京", "source": "amap-place-search"},
                        "semanticMetadata": {
                            "occurrenceId": "occ-meal-day-1",
                            "goalId": "goal_meal",
                            "planningSlotId": "slot-meal",
                        },
                    },
                ],
            }
        ],
    }
    artifact = {
        "context": {},
        "itinerarySnapshot": {},
        "planProposals": [{"snapshot_json": proposal_snapshot}],
        "planningSteps": [],
        "toolEvents": [],
        "verifierReport": {},
        "sessionSnapshot": {},
    }

    metrics = AgentQualityEvaluator(runtime=None)._metrics(artifact, {})

    assert metrics["goalOccurrencePlanCount"] == 1
    assert metrics["goalOccurrenceExpectedCount"] == metrics["goalOccurrenceActualCount"] == 2
    assert metrics["goalOccurrenceCoverageFailureCount"] == 0
    assert (
        metrics["groundedExplicitMealOccurrenceExpectedCount"]
        == metrics["groundedExplicitMealOccurrenceActualCount"]
        == 1
    )


def test_quality_evaluator_prefers_current_awaiting_portfolio_over_old_active_snapshot():
    def snapshot(occurrence_id: str, goal_id: str) -> dict:
        return {
            "portfolioGoalOccurrencePlan": {
                "occurrences": [
                    {
                        "occurrenceId": occurrence_id,
                        "sourceGoalId": goal_id,
                        "intentType": "museum",
                        "dayNumber": 1,
                        "requirementLevel": "hard",
                        "source": "controller_day_strategy",
                    }
                ]
            },
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "poi": {"amapId": f"poi-{occurrence_id}", "city": "北京", "source": "amap-place-search"},
                            "semanticMetadata": {
                                "occurrenceId": occurrence_id,
                                "goalId": goal_id,
                                "planningSlotId": f"slot-{occurrence_id}",
                            },
                        }
                    ],
                }
            ],
        }

    artifact = {
        "context": {},
        "itinerarySnapshot": snapshot("occ-old", "goal-old"),
        "planProposals": [{"id": "proposal-new", "snapshot_json": snapshot("occ-new", "goal-new")}],
        "portfolio": {"status": "awaiting_selection", "selected_proposal_id": None},
        "portfolioSelection": {},
        "planningSteps": [],
        "toolEvents": [],
        "verifierReport": {},
        "sessionSnapshot": {"conversation_session": {"active_version_id": "ver-old"}},
    }

    metrics = AgentQualityEvaluator(runtime=None)._metrics(artifact, {})

    assert metrics["proposalStageEvidenceUsed"] == 1
    assert metrics["goalOccurrenceExpectedCount"] == metrics["goalOccurrenceActualCount"] == 1
    assert metrics["controllerDayStrategyOccurrenceCount"] == 1


def test_quality_evaluator_uses_committed_final_snapshot_and_detects_selected_proposal_identity_drift():
    proposal_snapshot = {
        "portfolioGoalOccurrencePlan": {
            "occurrences": [
                {
                    "occurrenceId": "occ-new",
                    "sourceGoalId": "goal-new",
                    "intentType": "museum",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                    "source": "controller_day_strategy",
                }
            ]
        },
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "poi": {"amapId": "poi-proposal", "city": "北京", "source": "amap-place-search"},
                        "semanticMetadata": {
                            "occurrenceId": "occ-new",
                            "goalId": "goal-new",
                            "planningSlotId": "slot-new",
                        },
                    }
                ],
            }
        ],
    }
    final_snapshot = {
        "portfolioGoalOccurrencePlan": proposal_snapshot["portfolioGoalOccurrencePlan"],
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "poi": {"amapId": "poi-final", "city": "北京", "source": "amap-place-search"},
                        "semanticMetadata": {
                            "occurrenceId": "occ-new",
                            "goalId": "goal-new",
                            "planningSlotId": "slot-new",
                        },
                    }
                ],
            }
        ],
    }
    artifact = {
        "context": {},
        "itinerarySnapshot": final_snapshot,
        "planProposals": [{"id": "proposal-new", "snapshot_json": proposal_snapshot}],
        "portfolio": {"status": "selected", "selected_proposal_id": "proposal-new"},
        "portfolioSelection": {"selectedProposalId": "proposal-new", "activeVersionId": "ver-new"},
        "planningSteps": [],
        "toolEvents": [],
        "verifierReport": {},
        "sessionSnapshot": {},
    }

    metrics = AgentQualityEvaluator(runtime=None)._metrics(artifact, {})

    assert metrics["proposalStageEvidenceUsed"] == 0
    assert metrics["goalOccurrenceExpectedCount"] == metrics["goalOccurrenceActualCount"] == 1
    assert metrics["selectedProposalFinalOccurrenceMismatchCount"] == 0
    assert metrics["selectedProposalFinalIdentityMismatchCount"] == 1


def test_quality_evaluator_does_not_count_mock_amap_source_as_grounded_occurrence():
    artifact = {
        "context": {},
        "itinerarySnapshot": {
            "portfolioGoalOccurrencePlan": {
                "occurrences": [
                    {
                        "occurrenceId": "occ-mock",
                        "sourceGoalId": "goal-museum",
                        "intentType": "museum",
                        "dayNumber": 1,
                        "requirementLevel": "hard",
                    }
                ]
            },
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "poi": {
                                "amapId": "mock_amap_1",
                                "city": "北京",
                                "source": "mock-amap-place-search",
                                "sourceNote": "runtime eval mock",
                            },
                            "semanticMetadata": {
                                "occurrenceId": "occ-mock",
                                "goalId": "goal-museum",
                                "planningSlotId": "slot-mock",
                            },
                        }
                    ],
                }
            ],
        },
        "planningSteps": [],
        "toolEvents": [],
        "verifierReport": {},
        "sessionSnapshot": {},
    }

    metrics = AgentQualityEvaluator(runtime=None)._metrics(artifact, {})

    assert metrics["goalOccurrenceActualCount"] == 0
    assert metrics["goalOccurrenceCoverageFailureCount"] == 1
    assert metrics["occurrenceEvidenceMissingCount"] == 1


def test_quality_evaluator_requires_grounded_occurrence_city_to_match_target_city():
    artifact = {
        "context": {"selectedCity": "北京"},
        "itinerarySnapshot": {
            "portfolioGoalOccurrencePlan": {
                "occurrences": [
                    {
                        "occurrenceId": "occ-outside-city",
                        "sourceGoalId": "goal-museum",
                        "intentType": "museum",
                        "dayNumber": 1,
                        "requirementLevel": "hard",
                    }
                ]
            },
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "poi": {
                                "amapId": "real-amap-outside-city",
                                "city": "上海市",
                                "source": "amap-place-search",
                            },
                            "semanticMetadata": {
                                "occurrenceId": "occ-outside-city",
                                "goalId": "goal-museum",
                                "planningSlotId": "slot-outside-city",
                            },
                        }
                    ],
                }
            ],
        },
        "planningSteps": [],
        "toolEvents": [],
        "verifierReport": {},
        "sessionSnapshot": {},
    }

    metrics = AgentQualityEvaluator(runtime=None)._metrics(artifact, {})

    assert metrics["goalOccurrenceActualCount"] == 0
    assert metrics["goalOccurrenceCoverageFailureCount"] == 1
    assert metrics["occurrenceEvidenceMissingCount"] == 1
