from src.services.clarification_checkpoint_service import ClarificationCheckpointService


def _controller_question():
    return {
        "dimensionId": "night_view.cardinality",
        "question": "夜景安排的频次会影响每天的路线密度，你更倾向哪一种？",
        "whyItMatters": "不同频次需要不同数量的可验证地点与路线证据。",
        "allowFreeText": True,
        "options": [
            {"id": "one", "label": "安排一个晚上", "semanticValue": {"frequency": 1}},
            {"id": "each", "label": "每个可用晚上各安排一次", "semanticValue": {"frequency": "each_evening"}},
        ],
    }


def _request_contract():
    return {
        "clarificationReason": "night_view.cardinality",
        "requiredIntents": [
            {
                "intentType": "night_view",
                "allowedDayNumbers": [1, 2],
            }
        ],
        "experienceSpecs": [{"intentType": "night_view"}],
        "clarificationDimensions": [
            {
                "dimensionId": "night_view.cardinality",
                "intentType": "night_view",
                "status": "unresolved",
                "candidateScope": {
                    "intentType": "night_view",
                    "allowedDayNumbers": [1, 2],
                },
                "allowedSemanticFields": [
                    "frequency",
                    "occurrencePolicy",
                    "allowedDayNumbers",
                ],
            },
            {
                "dimensionId": "night_view.experience_mode",
                "intentType": "night_view",
                "status": "unresolved",
                "candidateScope": {
                    "intentType": "night_view",
                    "allowedDayNumbers": [1, 2],
                },
                "allowedSemanticFields": [
                    "experienceFamilies",
                    "accessPolicy",
                    "distinctnessPolicy",
                    "timeWindow",
                    "detourTolerance",
                    "evidenceFreshness",
                    "confidence",
                ],
            },
        ],
        "completionCriteria": [
            {
                "intentType": "night_view",
                "requiredResolvedDimensions": [
                    "night_view.cardinality",
                    "night_view.experience_mode",
                ],
            }
        ],
    }


def _bind_source(checkpoint, source_turn_id="turn_assistant"):
    checkpoint["sourceAssistantTurnId"] = source_turn_id
    checkpoint["fingerprint"] = ClarificationCheckpointService._fingerprint(
        {key: value for key, value in checkpoint.items() if key != "fingerprint"}
    )
    return checkpoint


def _controller_question_with_identity(checkpoint, **overrides):
    return {
        **_controller_question(),
        "checkpointId": checkpoint["checkpointId"],
        "planningRootId": checkpoint["planningRootId"],
        "requestFingerprint": checkpoint["requestFingerprint"],
        "checkpointFingerprint": checkpoint["fingerprint"],
        **overrides,
    }


def test_checkpoint_is_controller_authored_and_contains_a_machine_verifiable_contract():
    contract = _request_contract()

    checkpoint = ClarificationCheckpointService.create(
        planning_root_id="turn_root",
        request_contract=contract,
        controller_question=_controller_question(),
        source_user_turn_id="turn_user",
        candidate_gap_summary={"requiredGoal": "night_view", "missingOccurrences": 2},
    )

    assert checkpoint is not None
    assert checkpoint["checkpointId"].startswith("clarify_")
    assert checkpoint["status"] == "awaiting_answer"
    assert checkpoint["nextQuestionDimensionId"] == "night_view.cardinality"
    assert [item["dimensionId"] for item in checkpoint["ambiguities"]] == [
        "night_view.cardinality",
        "night_view.experience_mode",
    ]
    assert checkpoint["resolvedDimensions"] == []
    assert checkpoint["unresolvedDimensions"] == [
        "night_view.cardinality",
        "night_view.experience_mode",
    ]
    assert checkpoint["ambiguities"][0]["allowedSemanticValues"] == [{"frequency": 1}, {"frequency": "each_evening"}]
    assert checkpoint["candidateGapSummary"]["missingOccurrences"] == 2


def test_semantic_patch_rejects_explicit_null_fields_before_a_button_is_offered():
    assert not ClarificationCheckpointService.valid_semantic_patch({"frequency": None})
    assert not ClarificationCheckpointService.valid_semantic_patch(
        {"timeWindow": None},
        allowed_fields={"timeWindow"},
    )

    question = _controller_question()
    question["options"][0]["semanticValue"] = {"frequency": None}
    assert (
        ClarificationCheckpointService.create(
            planning_root_id="turn_root",
            request_contract=_request_contract(),
            controller_question=question,
            source_user_turn_id="turn_user",
        )
        is None
    )


def test_semantic_patch_rejects_noncanonical_mobility_enums():
    for mobility_profile in (
        {"transportMode": "driving", "paceClass": "fast"},
        {"transportMode": "drive", "paceClass": "standard"},
    ):
        assert not ClarificationCheckpointService.valid_semantic_patch(
            {"mobilityProfile": mobility_profile},
            allowed_fields={"mobilityProfile"},
        )


def test_first_question_rejects_model_minted_checkpoint_identity():
    question = {
        **_controller_question(),
        "checkpointId": "model_minted",
        "planningRootId": "model_root",
        "requestFingerprint": "model_request",
        "checkpointFingerprint": "model_checkpoint",
    }

    assert (
        ClarificationCheckpointService.create(
            planning_root_id="turn_root",
            request_contract=_request_contract(),
            controller_question=question,
            source_user_turn_id="turn_user",
        )
        is None
    )


def test_checkpoint_accepts_only_the_persisted_question_identity_and_semantic_value():
    contract = _request_contract()
    checkpoint = ClarificationCheckpointService.create(
        planning_root_id="turn_root",
        request_contract=contract,
        controller_question=_controller_question(),
        source_user_turn_id="turn_user",
    )
    assert checkpoint is not None
    _bind_source(checkpoint)

    resolved = ClarificationCheckpointService.resolve(
        checkpoint,
        checkpoint_id=checkpoint["checkpointId"],
        planning_root_id="turn_root",
        source_assistant_turn_id="turn_assistant",
        request_contract=contract,
        dimension_id="night_view.cardinality",
        semantic_value={"frequency": "each_evening"},
        source_user_turn_id="turn_answer",
        answer_source="structured_option",
    )

    assert resolved is not None
    assert resolved["status"] == "awaiting_agent_resolution"
    assert resolved["nextQuestionDimensionId"] is None
    assert resolved["resolvedAnswers"][-1]["semanticValue"] == {"frequency": "each_evening"}
    assert resolved["resolvedDimensions"] == ["night_view.cardinality"]
    assert resolved["unresolvedDimensions"] == ["night_view.experience_mode"]
    assert (
        ClarificationCheckpointService.resolve(
            resolved,
            checkpoint_id=resolved["checkpointId"],
            planning_root_id="turn_root",
            source_assistant_turn_id="turn_assistant",
            request_contract=contract,
            dimension_id="night_view.cardinality",
            semantic_value={"frequency": "each_evening"},
            source_user_turn_id="turn_duplicate",
            answer_source="structured_option",
        )
        is None
    )


def test_free_text_stays_pending_for_controller_semantic_resolution():
    contract = _request_contract()
    checkpoint = ClarificationCheckpointService.create(
        planning_root_id="turn_root",
        request_contract=contract,
        controller_question=_controller_question(),
        source_user_turn_id="turn_user",
    )
    assert checkpoint is not None
    _bind_source(checkpoint)

    pending = ClarificationCheckpointService.prepare_free_text(
        checkpoint,
        checkpoint_id=checkpoint["checkpointId"],
        planning_root_id="turn_root",
        source_assistant_turn_id="turn_assistant",
        request_contract=contract,
        dimension_id="night_view.cardinality",
        free_text="两个晚上都安排，但地点要不同",
        source_user_turn_id="turn_answer",
    )

    assert pending is not None
    assert pending["status"] == "awaiting_agent_resolution"
    assert pending["resolvedAnswers"] == []
    assert pending["pendingFreeTextAnswer"]["text"] == "两个晚上都安排，但地点要不同"
    assert (
        ClarificationCheckpointService.resolve(
            checkpoint,
            checkpoint_id=checkpoint["checkpointId"],
            planning_root_id="wrong_root",
            source_assistant_turn_id="turn_assistant",
            request_contract=contract,
            dimension_id="night_view.cardinality",
            semantic_value="1",
            source_user_turn_id="turn_answer",
            answer_source="structured_option",
        )
        is None
    )


def test_checkpoint_rejects_tampered_fingerprint_and_wrong_source_turn():
    contract = _request_contract()
    checkpoint = ClarificationCheckpointService.create(
        planning_root_id="turn_root",
        request_contract=contract,
        controller_question=_controller_question(),
        source_user_turn_id="turn_user",
    )
    assert checkpoint is not None
    _bind_source(checkpoint)
    checkpoint["question"]["question"] = "tampered"

    assert (
        ClarificationCheckpointService.resolve(
            checkpoint,
            checkpoint_id=checkpoint["checkpointId"],
            planning_root_id="turn_root",
            source_assistant_turn_id="turn_assistant",
            request_contract=contract,
            dimension_id="night_view.cardinality",
            semantic_value={"frequency": 1},
            source_user_turn_id="turn_answer",
            answer_source="structured_option",
        )
        is None
    )

    clean = ClarificationCheckpointService.create(
        planning_root_id="turn_root",
        request_contract=contract,
        controller_question=_controller_question(),
        source_user_turn_id="turn_user",
    )
    assert clean is not None
    _bind_source(clean)
    assert (
        ClarificationCheckpointService.prepare_free_text(
            clean,
            checkpoint_id=clean["checkpointId"],
            planning_root_id="turn_root",
            source_assistant_turn_id="wrong_assistant",
            request_contract=contract,
            dimension_id="night_view.cardinality",
            free_text="两个晚上",
            source_user_turn_id="turn_answer",
        )
        is None
    )


def test_changed_request_contract_cannot_graft_prior_answers_without_verified_transition():
    original_contract = _request_contract()
    checkpoint = ClarificationCheckpointService.create(
        planning_root_id="turn_root",
        request_contract=original_contract,
        controller_question=_controller_question(),
        source_user_turn_id="turn_user",
    )
    assert checkpoint is not None
    _bind_source(checkpoint)
    resolved = ClarificationCheckpointService.resolve(
        checkpoint,
        checkpoint_id=checkpoint["checkpointId"],
        planning_root_id="turn_root",
        source_assistant_turn_id="turn_assistant",
        request_contract=original_contract,
        dimension_id="night_view.cardinality",
        semantic_value={"frequency": "each_evening"},
        source_user_turn_id="turn_answer",
        answer_source="structured_option",
    )
    assert resolved is not None
    changed_contract = {
        **_request_contract(),
        "unrelatedInjectedConstraint": {"budget": "unbounded"},
    }
    second_question = _controller_question_with_identity(
        resolved,
        dimensionId="night_view.experience_mode",
        options=[
            {
                "id": "public",
                "label": "公共户外体验",
                "semanticValue": {
                    "accessPolicy": "public_outdoor",
                    "experienceFamilies": ["public_city_view"],
                },
            },
            {
                "id": "manual",
                "label": "我补充体验偏好",
                "semanticValue": {
                    "accessPolicy": "custom",
                    "experienceFamilies": ["public_city_view"],
                },
            },
        ],
    )

    assert (
        ClarificationCheckpointService.create(
            planning_root_id="turn_root",
            request_contract=changed_contract,
            controller_question=second_question,
            source_user_turn_id="turn_answer",
            prior_checkpoint=resolved,
        )
        is None
    )


def test_answered_checkpoint_advances_without_requiring_model_authored_opaque_lineage():
    original_contract = _request_contract()
    checkpoint = ClarificationCheckpointService.create(
        planning_root_id="turn_root",
        request_contract=original_contract,
        controller_question=_controller_question(),
        source_user_turn_id="turn_user",
    )
    assert checkpoint is not None
    _bind_source(checkpoint)
    resolved = ClarificationCheckpointService.resolve(
        checkpoint,
        checkpoint_id=checkpoint["checkpointId"],
        planning_root_id="turn_root",
        source_assistant_turn_id="turn_assistant",
        request_contract=original_contract,
        dimension_id="night_view.cardinality",
        semantic_value={"frequency": "each_evening"},
        source_user_turn_id="turn_answer",
        answer_source="structured_option",
    )
    assert resolved is not None
    advanced_contract = _request_contract()
    advanced_contract.update(
        {
            "clarificationCheckpointId": resolved["checkpointId"],
            "clarificationContractVersion": resolved["contractVersion"],
            "clarificationDecisionSource": "controller_semantic_choice",
            "clarificationAnswers": resolved["resolvedAnswers"],
            "experienceSpecs": resolved["experienceSpecs"],
        }
    )
    advanced_contract["clarificationDimensions"] = [
        {
            **item,
            "status": ("resolved" if item["dimensionId"] == "night_view.cardinality" else item["status"]),
        }
        for item in advanced_contract["clarificationDimensions"]
    ]
    second_question = _controller_question_with_identity(
        resolved,
        dimensionId="night_view.experience_mode",
        options=[
            {
                "id": "public",
                "label": "公共户外体验",
                "semanticValue": {
                    "accessPolicy": "public_outdoor",
                    "experienceFamilies": ["public_city_view"],
                },
            },
            {
                "id": "manual",
                "label": "我补充体验偏好",
                "semanticValue": {
                    "accessPolicy": "custom",
                    "experienceFamilies": ["public_city_view"],
                },
            },
        ],
    )
    for field in ("checkpointId", "planningRootId", "requestFingerprint", "checkpointFingerprint"):
        second_question.pop(field)

    advanced = ClarificationCheckpointService.create(
        planning_root_id="turn_root",
        request_contract=advanced_contract,
        controller_question=second_question,
        source_user_turn_id="turn_answer",
        prior_checkpoint=resolved,
    )

    assert advanced is not None
    assert advanced["checkpointId"] == resolved["checkpointId"]
    assert advanced["resolvedAnswers"] == resolved["resolvedAnswers"]
    assert advanced["resolvedDimensions"] == ["night_view.cardinality"]
    assert advanced["unresolvedDimensions"] == ["night_view.experience_mode"]
    assert advanced["nextQuestionDimensionId"] == "night_view.experience_mode"
    assert advanced["requestFingerprint"] == (ClarificationCheckpointService._fingerprint(advanced_contract))

    mismatched_question = {**second_question, "checkpointId": "stale_or_model_minted"}
    assert (
        ClarificationCheckpointService.create(
            planning_root_id="turn_root",
            request_contract=advanced_contract,
            controller_question=mismatched_question,
            source_user_turn_id="turn_answer",
            prior_checkpoint=resolved,
        )
        is None
    )


def test_candidate_gap_transition_requires_exact_gap_and_checkpoint_lineage():
    original_contract = _request_contract()
    checkpoint = ClarificationCheckpointService.create(
        planning_root_id="turn_root",
        request_contract=original_contract,
        controller_question=_controller_question(),
        source_user_turn_id="turn_user",
    )
    assert checkpoint is not None
    _bind_source(checkpoint)
    resolved = ClarificationCheckpointService.resolve(
        checkpoint,
        checkpoint_id=checkpoint["checkpointId"],
        planning_root_id="turn_root",
        source_assistant_turn_id="turn_assistant",
        request_contract=original_contract,
        dimension_id="night_view.cardinality",
        semantic_value={"frequency": "each_evening"},
        source_user_turn_id="turn_answer",
        answer_source="structured_option",
    )
    assert resolved is not None
    gap = {
        "schemaVersion": "candidate-gap-summary-v1",
        "status": "candidate_refresh_required",
        "missingOccurrenceCount": 1,
        "fingerprint": "candidate-gap-fingerprint",
    }
    with_gap = ClarificationCheckpointService.with_candidate_gap(resolved, gap)
    assert with_gap is not None
    gap_contract = _request_contract()
    gap_contract.update(
        {
            "clarificationCheckpointId": with_gap["checkpointId"],
            "clarificationContractVersion": with_gap["contractVersion"],
            "clarificationDecisionSource": "consumer_admission_candidate_gap",
            "clarificationAnswers": with_gap["resolvedAnswers"],
            "experienceSpecs": with_gap["experienceSpecs"],
            "candidateGapFingerprint": gap["fingerprint"],
        }
    )
    gap_contract["clarificationDimensions"] = [
        {
            **item,
            "status": ("resolved" if item["dimensionId"] == "night_view.cardinality" else item["status"]),
        }
        for item in gap_contract["clarificationDimensions"]
    ]
    gap_contract["clarificationDimensions"].append(
        {
            "dimensionId": "night_view.candidate_recovery",
            "intentType": "night_view",
            "status": "unresolved",
            "candidateScope": {"candidateGapFingerprint": gap["fingerprint"]},
            "allowedSemanticFields": ["accessPolicy", "experienceFamilies"],
        }
    )
    recovery_question = _controller_question_with_identity(
        with_gap,
        dimensionId="night_view.candidate_recovery",
        options=[
            {
                "id": "public",
                "label": "扩大到公共户外体验",
                "semanticValue": {
                    "accessPolicy": "public_outdoor",
                    "experienceFamilies": ["public_city_view"],
                },
            },
            {
                "id": "manual",
                "label": "我补充准入边界",
                "semanticValue": {
                    "accessPolicy": "custom",
                    "experienceFamilies": ["public_city_view"],
                },
            },
        ],
    )

    advanced = ClarificationCheckpointService.create(
        planning_root_id="turn_root",
        request_contract=gap_contract,
        controller_question=recovery_question,
        source_user_turn_id="turn_gap",
        prior_checkpoint=with_gap,
        candidate_gap_summary=gap,
    )

    assert advanced is not None
    assert advanced["checkpointId"] == with_gap["checkpointId"]
    assert (
        ClarificationCheckpointService.create(
            planning_root_id="turn_root",
            request_contract=gap_contract,
            controller_question=recovery_question,
            source_user_turn_id="turn_gap",
            prior_checkpoint=with_gap,
            candidate_gap_summary={**gap, "fingerprint": "wrong-gap"},
        )
        is None
    )


def test_checkpoint_rejects_poi_shaped_or_untyped_semantic_options():
    contract = _request_contract()
    base = {**_controller_question(), "dimensionId": "night_view.experience_mode"}
    invalid_values = [
        {"experienceFamilies": ["景山公园"]},
        {
            "timeWindow": {
                "start": "18:30",
                "end": "22:00",
                "placeName": "某观景台",
            }
        },
        {
            "evidenceFreshness": {
                "maxAgeHours": 24,
                "requiredForControlledAccess": "yes",
            }
        },
    ]

    for semantic_value in invalid_values:
        question = {
            **base,
            "options": [
                {
                    "id": "invalid",
                    "label": "非法语义",
                    "semanticValue": semantic_value,
                }
            ],
        }
        assert (
            ClarificationCheckpointService.create(
                planning_root_id="turn_root",
                request_contract=contract,
                controller_question=question,
                source_user_turn_id="turn_user",
            )
            is None
        )


def test_checkpoint_rejects_arbitrary_dimension_or_cross_dimension_semantics():
    contract = _request_contract()
    arbitrary = {**_controller_question(), "dimensionId": "night_view.server_script"}
    assert (
        ClarificationCheckpointService.create(
            planning_root_id="turn_root",
            request_contract=contract,
            controller_question=arbitrary,
            source_user_turn_id="turn_user",
        )
        is None
    )

    wrong_patch = {
        **_controller_question(),
        "options": [
            {
                "id": "public",
                "label": "公共空间",
                "semanticValue": {"accessPolicy": "public_outdoor"},
            },
            {
                "id": "controlled",
                "label": "受控空间",
                "semanticValue": {"accessPolicy": "verified_controlled_access"},
            },
        ],
    }
    assert (
        ClarificationCheckpointService.create(
            planning_root_id="turn_root",
            request_contract=contract,
            controller_question=wrong_patch,
            source_user_turn_id="turn_user",
        )
        is None
    )


def test_candidate_gap_updates_same_checkpoint_contract_and_fingerprint():
    checkpoint = ClarificationCheckpointService.create(
        planning_root_id="turn_root",
        request_contract=_request_contract(),
        controller_question=_controller_question(),
        source_user_turn_id="turn_user",
    )
    assert checkpoint is not None
    _bind_source(checkpoint)

    updated = ClarificationCheckpointService.with_candidate_gap(
        checkpoint,
        {
            "schemaVersion": "candidate-gap-summary-v1",
            "missingOccurrenceCount": 2,
            "rejectedReasonCounts": {"access_evidence_missing": 7},
        },
    )

    assert updated is not None
    assert updated["checkpointId"] == checkpoint["checkpointId"]
    assert updated["contractVersion"] == checkpoint["contractVersion"] + 1
    assert updated["candidateGapSummary"]["missingOccurrenceCount"] == 2
    assert updated["fingerprint"] != checkpoint["fingerprint"]


def test_custom_policy_value_remains_an_unresolved_machine_dimension():
    specs = ClarificationCheckpointService._experience_specs(
        request_contract={
            "requiredIntents": [
                {
                    "intentType": "night_view",
                    "allowedDayNumbers": [1, 2],
                }
            ]
        },
        answers=[
            {
                "dimensionId": "night_view.experience_mode",
                "semanticValue": {
                    "frequency": "every_available_evening",
                    "experienceFamilies": ["public_city_view"],
                    "accessPolicy": "custom",
                    "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
                    "timeWindow": {"start": "18:30", "end": "22:00"},
                    "detourTolerance": {
                        "maxGeneralizedCostDelta": 35,
                        "maxDetourRatio": 0.35,
                    },
                    "evidenceFreshness": {
                        "maxAgeHours": 24,
                        "requiredForControlledAccess": True,
                    },
                    "confidence": 0.8,
                },
            }
        ],
        ambiguities=[],
        existing=[],
    )

    assert specs[0]["unresolvedDimensions"] == ["night_view.accessPolicy"]


def test_semantic_patch_rejects_partial_time_window_and_zero_cost_delta():
    assert ClarificationCheckpointService._valid_semantic_patch({"timeWindow": {"start": "18:30"}}) is False
    assert (
        ClarificationCheckpointService._valid_semantic_patch(
            {
                "detourTolerance": {
                    "maxGeneralizedCostDelta": 0,
                    "maxDetourRatio": 0.35,
                }
            }
        )
        is False
    )
    assert (
        ClarificationCheckpointService._valid_semantic_patch(
            {
                "timeWindow": {"start": "18:30", "end": "22:00"},
                "detourTolerance": {
                    "maxGeneralizedCostDelta": 35,
                    "maxDetourRatio": 0.35,
                },
            }
        )
        is True
    )


def test_route_decision_answer_stays_out_of_experience_specs():
    initial_spec = {
        "intentType": "night_view",
        "frequency": "one",
        "allowedDayNumbers": [1, 2],
        "experienceFamilies": ["public_city_view"],
        "unresolvedDimensions": [],
        "source": "explicit_user_semantic_requirement",
    }
    contract = {
        "clarificationRequired": True,
        "clarificationReason": "route_decision.detour_tolerance",
        "requiredIntents": [
            {
                "intentType": "night_view",
                "requiredMin": 1,
                "allowedDayNumbers": [1, 2],
            }
        ],
        "experienceSpecs": [initial_spec],
        "clarificationDimensions": [
            {
                "dimensionId": "route_decision.detour_tolerance",
                "intentType": "route_decision",
                "status": "unresolved",
                "candidateScope": {"missingFields": ["detourTolerance"]},
                "allowedSemanticFields": ["detourTolerance"],
            }
        ],
    }
    question = {
        "dimensionId": "route_decision.detour_tolerance",
        "question": "这次行程可接受多大的路线绕行？",
        "whyItMatters": "路线矩阵需要一个用户选择的有界容忍度。",
        "allowFreeText": False,
        "options": [
            {
                "id": "bounded",
                "label": "较少绕行",
                "semanticValue": {
                    "detourTolerance": {
                        "maxGeneralizedCostDelta": 27.5,
                        "maxDetourRatio": 0.23,
                    }
                },
            },
            {
                "id": "wider",
                "label": "可接受更多绕行",
                "semanticValue": {
                    "detourTolerance": {
                        "maxGeneralizedCostDelta": 41.25,
                        "maxDetourRatio": 0.61,
                    }
                },
            },
        ],
    }
    checkpoint = ClarificationCheckpointService.create(
        planning_root_id="turn_root_route_decision",
        request_contract=contract,
        controller_question=question,
        source_user_turn_id="turn_user_question",
    )
    assert checkpoint is not None
    _bind_source(checkpoint, "turn_assistant_question")

    resolved = ClarificationCheckpointService.resolve(
        checkpoint,
        checkpoint_id=checkpoint["checkpointId"],
        planning_root_id=checkpoint["planningRootId"],
        source_assistant_turn_id=checkpoint["sourceAssistantTurnId"],
        request_contract=contract,
        dimension_id="route_decision.detour_tolerance",
        semantic_value=question["options"][0]["semanticValue"],
        source_user_turn_id="turn_user_answer",
        answer_source="structured_option",
    )

    assert resolved is not None
    assert resolved["resolvedAnswers"][-1] == {
        "dimensionId": "route_decision.detour_tolerance",
        "semanticValue": question["options"][0]["semanticValue"],
        "source": "structured_option",
        "sourceUserTurnId": "turn_user_answer",
    }
    assert resolved["experienceSpecs"] == [initial_spec]
    assert all(item.get("intentType") != "route_decision" for item in resolved["experienceSpecs"])


def test_route_decision_question_rejects_untyped_or_extra_semantics():
    contract = {
        "clarificationDimensions": [
            {
                "dimensionId": "route_decision.detour_tolerance",
                "status": "unresolved",
                "allowedSemanticFields": ["detourTolerance"],
            }
        ]
    }
    invalid_values = [
        {"detourTolerance": "moderate"},
        {
            "detourTolerance": {
                "maxGeneralizedCostDelta": 27.5,
                "maxDetourRatio": 0.23,
            },
            "label": "moderate",
        },
        {
            "detourTolerance": {
                "maxGeneralizedCostDelta": 27.5,
                "maxDetourRatio": 0.23,
                "mode": "transit",
            }
        },
        {
            "detourTolerance": {
                "maxGeneralizedCostDelta": 0,
                "maxDetourRatio": 0.23,
            }
        },
        {
            "detourTolerance": {
                "maxGeneralizedCostDelta": 27.5,
                "maxDetourRatio": -0.01,
            }
        },
    ]

    for index, semantic_value in enumerate(invalid_values):
        question = {
            "dimensionId": "route_decision.detour_tolerance",
            "question": "这次行程可接受多大的路线绕行？",
            "whyItMatters": "路线矩阵需要一个用户选择的有界容忍度。",
            "allowFreeText": False,
            "options": [
                {"id": f"invalid_{index}", "label": "无效选项", "semanticValue": semantic_value},
                {
                    "id": f"valid_{index}",
                    "label": "有效选项",
                    "semanticValue": {
                        "detourTolerance": {
                            "maxGeneralizedCostDelta": 27.5,
                            "maxDetourRatio": 0.23,
                        }
                    },
                },
            ],
        }
        assert (
            ClarificationCheckpointService.create(
                planning_root_id="turn_root_route_decision",
                request_contract=contract,
                controller_question=question,
                source_user_turn_id="turn_user_question",
            )
            is None
        )
