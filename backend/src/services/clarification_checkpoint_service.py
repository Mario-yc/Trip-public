"""Durable, controller-authored clarification contracts.

The checkpoint is stored with the assistant turn and copied into the next user
turn's request context by the existing conversation persistence path.  It does
not write an itinerary, create a second planner, or infer business answers from
button labels.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Optional
from uuid import uuid4


class ClarificationCheckpointService:
    LEGACY_SCHEMA_VERSION = "clarification-checkpoint-v1"
    SCHEMA_VERSION = "clarification-checkpoint-v2"
    SUPPORTED_SCHEMA_VERSIONS = {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}
    PERSISTED_OPTION_ONLY_DIMENSIONS = frozenset(
        {"route_decision.detour_tolerance"}
    )
    SEMANTIC_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
        "intentType": {
            "type": "token",
            "example": "night_view",
        },
        "frequency": {
            "type": "positive_integer_or_token",
            "examples": [2, "every_available_evening"],
        },
        "occurrencePolicy": {
            "type": "token",
            "example": "every_allowed_day",
        },
        "allowedDayNumbers": {
            "type": "nonempty_positive_integer_array",
            "example": [1, 2],
        },
        "experienceFamilies": {
            "type": "nonempty_token_array",
            "example": ["public_city_view", "waterfront_evening"],
        },
        "accessPolicy": {
            "type": "token",
            "example": "public_outdoor_or_verified_controlled_access",
        },
        "distinctnessPolicy": {
            "type": "token",
            "example": "distinct_physical_identity_per_occurrence",
        },
        "timeWindow": {
            "type": "object",
            "requiredKeys": ["start", "end"],
            "additionalKeys": False,
            "valueFormat": "HH:mm",
        },
        "detourTolerance": {
            "type": "object",
            "requiredKeys": ["maxGeneralizedCostDelta", "maxDetourRatio"],
            "additionalKeys": False,
            "example": {"maxGeneralizedCostDelta": 35, "maxDetourRatio": 0.35},
        },
        "adjacentLegConstraint": {
            "type": "object",
            "requiredKeys": ["candidateSearchRadiusMeters", "maxProviderTravelMinutes"],
            "additionalKeys": False,
            "example": {"candidateSearchRadiusMeters": 5000, "maxProviderTravelMinutes": 45},
        },
        "spatialResolutionInput": {
            "type": "object",
            "controllerAuthorableKinds": [
                "reference_point",
                "reference_point_radius",
                "administrative_area",
                "named_boundary",
            ],
            "variants": {
                "reference_point": {
                    "requiredKeys": ["kind", "referenceText"],
                    "additionalKeys": False,
                    "kind": "reference_point",
                    "referenceText": {"type": "nonempty_text", "maxLength": 80},
                },
                "reference_point_radius": {
                    "requiredKeys": ["kind", "referenceText", "radiusMeters"],
                    "additionalKeys": False,
                    "kind": "reference_point_radius",
                    "referenceText": {"type": "nonempty_text", "maxLength": 80},
                    "radiusMeters": {"type": "number", "minimum": 100, "maximum": 50000},
                },
                "administrative_area": {
                    "requiredKeys": ["kind", "administrativeAreaText"],
                    "additionalKeys": False,
                    "kind": "administrative_area",
                    "administrativeAreaText": {"type": "nonempty_text", "maxLength": 80},
                },
                "named_boundary": {
                    "requiredKeys": ["kind", "boundaryText", "containment"],
                    "additionalKeys": False,
                    "kind": "named_boundary",
                    "boundaryText": {"type": "nonempty_text", "maxLength": 80},
                    "containment": {"enum": ["inside", "outside"]},
                },
            },
            "checkpointBoundKind": {
                "kind": "map_selection",
                "requiredKeys": ["kind", "mapSelectionFingerprint"],
                "authoringPolicy": "server_checkpoint_only",
            },
            "identityPolicy": "provider_grounding_required",
            "forbiddenKeys": ["amapId", "longitude", "latitude", "coordinates", "adcode"],
        },
        "mobilityProfile": {
            "type": "object",
            "requiredKeys": ["transportMode", "paceClass"],
            "additionalKeys": False,
            "properties": {
                "transportMode": {
                    "type": "token",
                    "enum": ["transit", "public_transit", "driving", "walking", "bicycling"],
                },
                "paceClass": {
                    "type": "token",
                    "enum": ["relaxed", "standard", "intensive"],
                },
            },
            "example": {"transportMode": "transit", "paceClass": "standard"},
        },
        "evidenceFreshness": {
            "type": "object",
            "requiredKeys": ["maxAgeHours"],
            "optionalBooleanKeys": [
                "requiredForControlledAccess",
                "requiredForPublicOutdoor",
                "allowExplicitNoClosure",
            ],
            "example": {"maxAgeHours": 24, "requiredForControlledAccess": True},
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "example": 0.9,
        },
    }

    @classmethod
    def semantic_field_schemas(cls, fields: Any) -> dict[str, dict[str, Any]]:
        """Project the exact server validator contract for Controller authorship."""

        if not isinstance(fields, list):
            return {}
        return {
            str(field): copy.deepcopy(cls.SEMANTIC_FIELD_SCHEMAS[str(field)])
            for field in fields
            if str(field) in cls.SEMANTIC_FIELD_SCHEMAS
        }

    @classmethod
    def valid_semantic_patch(
        cls,
        value: Any,
        *,
        allowed_fields: Optional[set[str]] = None,
    ) -> bool:
        if not cls._valid_semantic_patch(value):
            return False
        return allowed_fields is None or set(value).issubset(allowed_fields)

    @classmethod
    def valid_controller_semantic_patch(
        cls,
        value: Any,
        *,
        allowed_fields: Optional[set[str]] = None,
    ) -> bool:
        """Validate model-authored semantics without accepting server identities."""

        if not cls.valid_semantic_patch(value, allowed_fields=allowed_fields):
            return False
        spatial_input = value.get("spatialResolutionInput") if isinstance(value, dict) else None
        return not (isinstance(spatial_input, dict) and str(spatial_input.get("kind") or "") == "map_selection")

    @classmethod
    def create(
        cls,
        *,
        planning_root_id: str,
        request_contract: dict[str, Any],
        controller_question: dict[str, Any],
        source_user_turn_id: str,
        prior_checkpoint: Optional[dict[str, Any]] = None,
        candidate_gap_summary: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Create one opaque checkpoint from a small Controller question batch.

        The Controller may author up to three related questions in one decision.
        New batches are exposed and resolved atomically under one opaque submit
        capability; legacy single-question checkpoints remain readable. A
        missing/invalid batch returns ``None`` so callers keep current state
        recoverable rather than emitting server-authored business copy.
        """
        previous = prior_checkpoint if isinstance(prior_checkpoint, dict) else {}
        request_fingerprint = cls._fingerprint(request_contract)
        identity_fields = (
            "checkpointId",
            "planningRootId",
            "requestFingerprint",
            "checkpointFingerprint",
        )
        if previous:
            if not cls._valid_prior_checkpoint(
                previous,
                planning_root_id=planning_root_id,
                request_contract=request_contract,
                candidate_gap_summary=candidate_gap_summary,
            ) or not cls._controller_identity_matches_checkpoint(
                controller_question,
                previous,
            ):
                return None
        elif any(controller_question.get(field) not in (None, "") for field in identity_fields):
            # The model cannot mint the first durable identity.  It is created
            # below only after the controller-authored question is validated.
            return None
        supplied_questions = controller_question.get("questions")
        explicit_batch = isinstance(supplied_questions, list) and bool(supplied_questions)
        batch = (
            [copy.deepcopy(item) for item in supplied_questions if isinstance(item, dict)]
            if isinstance(supplied_questions, list)
            else []
        )
        if not batch:
            batch = [copy.deepcopy(controller_question)]
        if not 1 <= len(batch) <= 3:
            return None
        if len({str(item.get("dimensionId") or item.get("dimension_id") or "").strip() for item in batch}) != len(
            batch
        ):
            return None
        first_question = batch[0]
        question = str(first_question.get("question") or "").strip()
        dimension_id = str(first_question.get("dimensionId") or first_question.get("dimension_id") or "").strip()
        why_it_matters = str(first_question.get("whyItMatters") or "").strip()
        allow_free_text = first_question.get("allowFreeText")
        dimension = cls._unresolved_dimension_contract(
            request_contract,
            dimension_id,
        )
        options = cls._options(first_question.get("options"))
        allowed_semantic_fields = {
            str(item) for item in (dimension or {}).get("allowedSemanticFields") or [] if str(item).strip()
        }
        if (
            not question
            or not dimension_id
            or not why_it_matters
            or not isinstance(allow_free_text, bool)
            or dimension is None
            or len(options) < 2
            or not allowed_semantic_fields
            or any(
                not cls.valid_controller_semantic_patch(
                    option["semanticValue"],
                    allowed_fields=allowed_semantic_fields,
                )
                for option in options
            )
        ):
            return None
        if dimension_id in cls.PERSISTED_OPTION_ONLY_DIMENSIONS:
            allow_free_text = False
        pending_questions: list[dict[str, Any]] = []
        for pending_question in batch[1:]:
            pending_dimension_id = str(
                pending_question.get("dimensionId") or pending_question.get("dimension_id") or ""
            ).strip()
            pending_dimension = cls._unresolved_dimension_contract(request_contract, pending_dimension_id)
            pending_options = cls._options(pending_question.get("options"))
            pending_allowed_fields = {
                str(item) for item in (pending_dimension or {}).get("allowedSemanticFields") or [] if str(item).strip()
            }
            normalized_pending = {
                "dimensionId": pending_dimension_id,
                "question": str(pending_question.get("question") or "").strip(),
                "whyItMatters": str(pending_question.get("whyItMatters") or "").strip(),
                "options": pending_options,
                "allowFreeText": pending_question.get("allowFreeText"),
            }
            if (
                pending_dimension is None
                or not normalized_pending["question"]
                or not normalized_pending["whyItMatters"]
                or not isinstance(normalized_pending["allowFreeText"], bool)
                or len(pending_options) < 2
                or not pending_allowed_fields
                or any(
                    not cls.valid_controller_semantic_patch(
                        option["semanticValue"],
                        allowed_fields=pending_allowed_fields,
                    )
                    for option in pending_options
                )
            ):
                return None
            if pending_dimension_id in cls.PERSISTED_OPTION_ONLY_DIMENSIONS:
                normalized_pending["allowFreeText"] = False
            pending_questions.append(normalized_pending)
        prior_answers = [
            copy.deepcopy(item) for item in previous.get("resolvedAnswers") or [] if isinstance(item, dict)
        ]
        resolved_answer_dimensions = {
            str(item.get("dimensionId") or "")
            for item in prior_answers
            if str(item.get("dimensionId") or "")
        }
        resolved_question_contract_by_dimension = {
            str(item.get("dimensionId") or ""): copy.deepcopy(item)
            for item in previous.get("resolvedQuestionContracts") or []
            if isinstance(item, dict) and str(item.get("dimensionId") or "")
        }
        for prior_question in previous.get("questions") or []:
            if not isinstance(prior_question, dict):
                continue
            prior_dimension_id = str(prior_question.get("dimensionId") or "")
            if prior_dimension_id not in resolved_answer_dimensions:
                continue
            resolved_question_contract_by_dimension[prior_dimension_id] = {
                "dimensionId": prior_dimension_id,
                "allowFreeText": prior_question.get("allowFreeText"),
                "options": copy.deepcopy(prior_question.get("options") or []),
            }
        normalized_questions = [
            {
                "dimensionId": dimension_id,
                "question": question,
                "whyItMatters": why_it_matters,
                "options": copy.deepcopy(options),
                "allowFreeText": allow_free_text,
                "required": True,
            },
            *[{**copy.deepcopy(item), "required": True} for item in pending_questions],
        ]
        ambiguities = cls._ambiguity_state(
            request_contract=request_contract,
            prior_checkpoint=previous,
            answers=prior_answers,
            current_dimension_id=dimension_id,
            current_impact=why_it_matters,
            current_options=options,
        )
        batch_question_by_dimension = {
            str(item.get("dimensionId") or ""): item
            for item in normalized_questions
            if str(item.get("dimensionId") or "")
        }
        ambiguities = [
            {
                **item,
                "impact": str(batch_question_by_dimension[item["dimensionId"]].get("whyItMatters") or ""),
                "allowedSemanticValues": copy.deepcopy(
                    [
                        option["semanticValue"]
                        for option in batch_question_by_dimension[item["dimensionId"]].get("options") or []
                        if isinstance(option, dict) and "semanticValue" in option
                    ]
                ),
            }
            if str(item.get("dimensionId") or "") in batch_question_by_dimension
            else item
            for item in ambiguities
        ]
        resolved_dimensions = [item["dimensionId"] for item in ambiguities if item.get("resolved") is True]
        unresolved_dimensions = [item["dimensionId"] for item in ambiguities if item.get("resolved") is not True]
        checkpoint_id = str(previous.get("checkpointId") or f"clarify_{uuid4().hex[:16]}")
        # A Controller that follows the new ``questions`` contract receives v2
        # even for a one-question batch. Legacy single-question controller
        # output and persisted history remain executable through v1.
        schema_version = (
            cls.SCHEMA_VERSION if explicit_batch else str(previous.get("schemaVersion") or cls.LEGACY_SCHEMA_VERSION)
        )
        if schema_version not in cls.SUPPORTED_SCHEMA_VERSIONS:
            return None
        checkpoint = {
            "schemaVersion": schema_version,
            "checkpointId": checkpoint_id,
            "planningRootId": str(planning_root_id),
            "requestFingerprint": request_fingerprint,
            "contractVersion": int(previous.get("contractVersion") or 0) + 1,
            "ambiguities": ambiguities,
            "resolvedDimensions": resolved_dimensions,
            "unresolvedDimensions": unresolved_dimensions,
            "resolvedAnswers": prior_answers,
            "resolvedQuestionContracts": [
                resolved_question_contract_by_dimension[dimension_id]
                for dimension_id in sorted(resolved_question_contract_by_dimension)
            ],
            "experienceSpecs": copy.deepcopy(
                previous.get("experienceSpecs") or request_contract.get("experienceSpecs") or []
            ),
            "candidateGapSummary": copy.deepcopy(candidate_gap_summary or previous.get("candidateGapSummary") or {}),
            "nextQuestionDimensionId": dimension_id,
            "completionCriteria": copy.deepcopy(request_contract.get("completionCriteria") or []),
            "status": "awaiting_answer",
            "sourceUserTurnId": str(source_user_turn_id),
            "sourceAssistantTurnId": "",
            "question": {
                "dimensionId": dimension_id,
                "question": question,
                "whyItMatters": why_it_matters,
                "options": options,
                "allowFreeText": allow_free_text,
            },
            "pendingQuestions": pending_questions,
            "questionBatchSource": "model_controller",
        }
        if schema_version == cls.SCHEMA_VERSION:
            checkpoint.update(
                {
                    "submissionMode": "batch_atomic",
                    "questions": normalized_questions,
                    "submitChoiceId": f"clarification-batch:{checkpoint_id}",
                }
            )
        checkpoint["fingerprint"] = cls._fingerprint(checkpoint)
        return checkpoint

    @classmethod
    def with_candidate_gap(
        cls,
        checkpoint: dict[str, Any],
        candidate_gap_summary: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Attach grounded admission evidence without creating another state source."""

        if not isinstance(candidate_gap_summary, dict) or not candidate_gap_summary:
            return None
        fingerprint = str(checkpoint.get("fingerprint") or "") if isinstance(checkpoint, dict) else ""
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("schemaVersion") not in cls.SUPPORTED_SCHEMA_VERSIONS
            or not fingerprint
            or fingerprint
            != cls._fingerprint({key: value for key, value in checkpoint.items() if key != "fingerprint"})
        ):
            return None
        updated = copy.deepcopy(checkpoint)
        updated["candidateGapSummary"] = copy.deepcopy(candidate_gap_summary)
        updated["contractVersion"] = int(updated.get("contractVersion") or 0) + 1
        updated["fingerprint"] = cls._fingerprint(
            {key: value for key, value in updated.items() if key != "fingerprint"}
        )
        return updated

    @classmethod
    def resolve(
        cls,
        checkpoint: dict[str, Any],
        *,
        checkpoint_id: str,
        planning_root_id: str,
        source_assistant_turn_id: str,
        request_contract: dict[str, Any],
        dimension_id: str,
        semantic_value: Any,
        source_user_turn_id: str,
        answer_source: str,
    ) -> Optional[dict[str, Any]]:
        if not cls._matches_current_question(
            checkpoint,
            checkpoint_id=checkpoint_id,
            planning_root_id=planning_root_id,
            source_assistant_turn_id=source_assistant_turn_id,
            request_contract=request_contract,
            dimension_id=dimension_id,
        ):
            return None
        question = checkpoint.get("question") if isinstance(checkpoint.get("question"), dict) else {}
        allowed = {
            cls._semantic_key(item.get("semanticValue"))
            for item in question.get("options") or []
            if isinstance(item, dict)
        }
        value_key = cls._semantic_key(semantic_value)
        if not value_key or (answer_source == "structured_option" and value_key not in allowed):
            return None
        resolved = copy.deepcopy(checkpoint)
        answers = [item for item in resolved.get("resolvedAnswers") or [] if isinstance(item, dict)]
        answers.append(
            {
                "dimensionId": str(dimension_id),
                "semanticValue": copy.deepcopy(semantic_value),
                "source": str(answer_source),
                "sourceUserTurnId": str(source_user_turn_id),
            }
        )
        resolved["resolvedAnswers"] = answers
        resolved["ambiguities"] = [
            {**item, "resolved": True, "answerSource": str(answer_source)}
            if str(item.get("dimensionId") or "") == str(dimension_id)
            else item
            for item in resolved.get("ambiguities") or []
            if isinstance(item, dict)
        ]
        resolved["resolvedDimensions"] = [
            str(item.get("dimensionId") or "")
            for item in resolved["ambiguities"]
            if item.get("resolved") is True and str(item.get("dimensionId") or "")
        ]
        resolved["unresolvedDimensions"] = [
            str(item.get("dimensionId") or "")
            for item in resolved["ambiguities"]
            if item.get("resolved") is not True and str(item.get("dimensionId") or "")
        ]
        resolved["contractVersion"] = int(resolved.get("contractVersion") or 0) + 1
        resolved["experienceSpecs"] = cls._experience_specs(
            request_contract=request_contract,
            answers=answers,
            ambiguities=resolved["ambiguities"],
            existing=resolved.get("experienceSpecs"),
        )
        pending_questions = [
            copy.deepcopy(item)
            for item in resolved.get("pendingQuestions") or []
            if isinstance(item, dict) and str(item.get("dimensionId") or "") in set(resolved["unresolvedDimensions"])
        ]
        if pending_questions:
            next_question = pending_questions.pop(0)
            resolved["question"] = next_question
            resolved["pendingQuestions"] = pending_questions
            resolved["nextQuestionDimensionId"] = str(next_question.get("dimensionId") or "")
            resolved["status"] = "awaiting_answer"
        # ``answered`` is a terminal checkpoint state, not merely "the most
        # recent question was answered".  Keeping unresolved dimensions under
        # that terminal label allowed downstream planning to skip required
        # questions nondeterministically.  The server must author/bind the next
        # question before the checkpoint can return to ``awaiting_answer``.
        elif resolved["unresolvedDimensions"]:
            resolved["status"] = "awaiting_agent_resolution"
            resolved["nextQuestionDimensionId"] = None
        else:
            resolved["status"] = "answered"
            resolved["nextQuestionDimensionId"] = None
        resolved["sourceUserTurnId"] = str(source_user_turn_id)
        resolved["fingerprint"] = cls._fingerprint(
            {key: value for key, value in resolved.items() if key != "fingerprint"}
        )
        return resolved

    @classmethod
    def resolve_batch(
        cls,
        checkpoint: dict[str, Any],
        *,
        checkpoint_id: str,
        planning_root_id: str,
        source_assistant_turn_id: str,
        request_contract: dict[str, Any],
        selections: Any,
        source_user_turn_id: str,
        normalized_manual_answers: Optional[dict[str, dict[str, Any]]] = None,
    ) -> Optional[dict[str, Any]]:
        """Resolve one persisted Controller question batch as an all-or-none unit.

        The client submits only opaque option identities. Semantic patches are
        recovered from the persisted checkpoint and are applied to a copy only
        after the complete batch passes identity and coverage validation.
        """

        if not isinstance(checkpoint, dict) or checkpoint.get("schemaVersion") != cls.SCHEMA_VERSION:
            return None
        fingerprint = str(checkpoint.get("fingerprint") or "")
        if (
            str(checkpoint.get("status") or "") != "awaiting_answer"
            or not fingerprint
            or fingerprint
            != cls._fingerprint({key: value for key, value in checkpoint.items() if key != "fingerprint"})
            or str(checkpoint.get("checkpointId") or "") != str(checkpoint_id or "")
            or str(checkpoint.get("planningRootId") or "") != str(planning_root_id or "")
            or str(checkpoint.get("sourceAssistantTurnId") or "") != str(source_assistant_turn_id or "")
            or str(checkpoint.get("requestFingerprint") or "") != cls._fingerprint(request_contract)
            or checkpoint.get("submissionMode") != "batch_atomic"
        ):
            return None
        questions = [
            copy.deepcopy(item)
            for item in checkpoint.get("questions") or []
            if isinstance(item, dict) and str(item.get("dimensionId") or "")
        ]
        if not 1 <= len(questions) <= 3 or not isinstance(selections, list) or len(selections) != len(questions):
            return None
        question_by_dimension = {str(item["dimensionId"]): item for item in questions}
        if len(question_by_dimension) != len(questions):
            return None
        supplied_dimensions = [
            str(item.get("dimensionId") or "").strip() for item in selections if isinstance(item, dict)
        ]
        if (
            len(supplied_dimensions) != len(selections)
            or len(set(supplied_dimensions)) != len(supplied_dimensions)
            or set(supplied_dimensions) != set(question_by_dimension)
        ):
            return None

        dimension_contracts = {
            str(item.get("dimensionId") or ""): item
            for item in request_contract.get("clarificationDimensions") or []
            if isinstance(item, dict) and str(item.get("dimensionId") or "")
        }
        normalized_answers: list[dict[str, Any]] = []
        normalized_manual_answers = normalized_manual_answers or {}
        for selection in selections:
            if not isinstance(selection, dict):
                return None
            dimension_id = str(selection.get("dimensionId") or "").strip()
            option_id = str(selection.get("optionId") or "").strip()
            manual_value = str(selection.get("manualValue") or "").strip()
            question = question_by_dimension.get(dimension_id)
            dimension_contract = dimension_contracts.get(dimension_id)
            if question is None or dimension_contract is None or bool(option_id) == bool(manual_value):
                return None
            option = None
            answer_source = "structured_option"
            if option_id:
                option = next(
                    (
                        item
                        for item in question.get("options") or []
                        if isinstance(item, dict) and str(item.get("id") or "") == option_id
                    ),
                    None,
                )
                if option is None:
                    return None
                semantic_value = copy.deepcopy(option.get("semanticValue"))
            else:
                if question.get("allowFreeText") is not True or len(manual_value) > 800:
                    return None
                semantic_value = copy.deepcopy(normalized_manual_answers.get(dimension_id))
                answer_source = "free_text_normalized"
            allowed_fields = {
                str(item) for item in dimension_contract.get("allowedSemanticFields") or [] if str(item).strip()
            }
            if not cls.valid_semantic_patch(semantic_value, allowed_fields=allowed_fields):
                return None
            normalized_answers.append(
                {
                    "dimensionId": dimension_id,
                    "semanticValue": semantic_value,
                    "source": answer_source,
                    "sourceUserTurnId": str(source_user_turn_id),
                    "optionId": option_id or None,
                    "label": str(option.get("label") or "") if option is not None else manual_value,
                }
            )

        resolved = copy.deepcopy(checkpoint)
        prior_answers = [
            copy.deepcopy(item) for item in resolved.get("resolvedAnswers") or [] if isinstance(item, dict)
        ]
        if set(question_by_dimension).intersection(str(item.get("dimensionId") or "") for item in prior_answers):
            return None
        answers = [*prior_answers, *normalized_answers]
        resolved["resolvedAnswers"] = answers
        answered_dimensions = {item["dimensionId"] for item in normalized_answers}
        answer_source_by_dimension = {
            str(item.get("dimensionId") or ""): str(item.get("source") or "") for item in normalized_answers
        }
        resolved["ambiguities"] = [
            {
                **item,
                "resolved": True,
                "answerSource": answer_source_by_dimension.get(str(item.get("dimensionId") or "")),
            }
            if str(item.get("dimensionId") or "") in answered_dimensions
            else item
            for item in resolved.get("ambiguities") or []
            if isinstance(item, dict)
        ]
        resolved["resolvedDimensions"] = [
            str(item.get("dimensionId") or "")
            for item in resolved["ambiguities"]
            if item.get("resolved") is True and str(item.get("dimensionId") or "")
        ]
        resolved["unresolvedDimensions"] = [
            str(item.get("dimensionId") or "")
            for item in resolved["ambiguities"]
            if item.get("resolved") is not True and str(item.get("dimensionId") or "")
        ]
        resolved["contractVersion"] = int(resolved.get("contractVersion") or 0) + 1
        resolved["experienceSpecs"] = cls._experience_specs(
            request_contract=request_contract,
            answers=answers,
            ambiguities=resolved["ambiguities"],
            existing=resolved.get("experienceSpecs"),
        )
        resolved["pendingQuestions"] = []
        resolved["nextQuestionDimensionId"] = None
        resolved["status"] = "awaiting_agent_resolution" if resolved["unresolvedDimensions"] else "answered"
        resolved["sourceUserTurnId"] = str(source_user_turn_id)
        resolved["fingerprint"] = cls._fingerprint(
            {key: value for key, value in resolved.items() if key != "fingerprint"}
        )
        return resolved

    @classmethod
    def manual_normalization_context(
        cls,
        checkpoint: dict[str, Any],
        *,
        checkpoint_id: str,
        planning_root_id: str,
        source_assistant_turn_id: str,
        request_contract: dict[str, Any],
        selections: Any,
    ) -> Optional[dict[str, Any]]:
        """Return a bounded Controller input only after capability and batch shape validation."""

        if not isinstance(checkpoint, dict) or checkpoint.get("schemaVersion") != cls.SCHEMA_VERSION:
            return None
        fingerprint = str(checkpoint.get("fingerprint") or "")
        if (
            str(checkpoint.get("status") or "") != "awaiting_answer"
            or not fingerprint
            or fingerprint
            != cls._fingerprint({key: value for key, value in checkpoint.items() if key != "fingerprint"})
            or str(checkpoint.get("checkpointId") or "") != str(checkpoint_id or "")
            or str(checkpoint.get("planningRootId") or "") != str(planning_root_id or "")
            or str(checkpoint.get("sourceAssistantTurnId") or "") != str(source_assistant_turn_id or "")
            or str(checkpoint.get("requestFingerprint") or "") != cls._fingerprint(request_contract)
            or checkpoint.get("submissionMode") != "batch_atomic"
        ):
            return None
        questions = [
            item
            for item in checkpoint.get("questions") or []
            if isinstance(item, dict) and str(item.get("dimensionId") or "")
        ]
        if not 1 <= len(questions) <= 3 or not isinstance(selections, list) or len(selections) != len(questions):
            return None
        question_by_dimension = {str(item["dimensionId"]): item for item in questions}
        dimension_contracts = {
            str(item.get("dimensionId") or ""): item
            for item in request_contract.get("clarificationDimensions") or []
            if isinstance(item, dict) and str(item.get("dimensionId") or "")
        }
        supplied: set[str] = set()
        manual_questions: list[dict[str, Any]] = []
        for selection in selections:
            if not isinstance(selection, dict):
                return None
            dimension_id = str(selection.get("dimensionId") or "").strip()
            option_id = str(selection.get("optionId") or "").strip()
            manual_value = str(selection.get("manualValue") or "").strip()
            if dimension_id in supplied or bool(option_id) == bool(manual_value):
                return None
            supplied.add(dimension_id)
            question = question_by_dimension.get(dimension_id)
            contract = dimension_contracts.get(dimension_id)
            if question is None or contract is None:
                return None
            if option_id:
                if not any(
                    isinstance(item, dict) and str(item.get("id") or "") == option_id
                    for item in question.get("options") or []
                ):
                    return None
                continue
            if question.get("allowFreeText") is not True or len(manual_value) > 800:
                return None
            allowed_fields = [str(item) for item in contract.get("allowedSemanticFields") or [] if str(item).strip()]
            if not allowed_fields:
                return None
            manual_questions.append(
                {
                    "dimensionId": dimension_id,
                    "question": str(question.get("question") or ""),
                    "manualValue": manual_value,
                    "allowedSemanticFields": allowed_fields,
                }
            )
        if supplied != set(question_by_dimension) or not manual_questions:
            return None
        return {
            "schemaVersion": "clarification-batch-normalization-context-v1",
            "questions": manual_questions,
        }

    @classmethod
    def prepare_free_text(
        cls,
        checkpoint: dict[str, Any],
        *,
        checkpoint_id: str,
        planning_root_id: str,
        source_assistant_turn_id: str,
        request_contract: dict[str, Any],
        dimension_id: str,
        free_text: str,
        source_user_turn_id: str,
    ) -> Optional[dict[str, Any]]:
        """Keep raw prose pending until the controller normalizes its meaning.

        The server validates only checkpoint identity here.  It never maps the
        user's prose to an allowed business value; the next controller turn
        must emit a new structured question before the ambiguity can resolve.
        """
        text = str(free_text or "").strip()
        if not text or not cls._matches_current_question(
            checkpoint,
            checkpoint_id=checkpoint_id,
            planning_root_id=planning_root_id,
            source_assistant_turn_id=source_assistant_turn_id,
            request_contract=request_contract,
            dimension_id=dimension_id,
            require_free_text=True,
        ):
            return None
        pending = copy.deepcopy(checkpoint)
        pending["status"] = "awaiting_agent_resolution"
        pending["pendingFreeTextAnswer"] = {
            "dimensionId": str(dimension_id),
            "text": text[:800],
            "sourceUserTurnId": str(source_user_turn_id),
            "source": "free_text",
        }
        pending["sourceUserTurnId"] = str(source_user_turn_id)
        pending["fingerprint"] = cls._fingerprint(
            {key: value for key, value in pending.items() if key != "fingerprint"}
        )
        return pending

    @classmethod
    def _matches_current_question(
        cls,
        checkpoint: dict[str, Any],
        *,
        checkpoint_id: str,
        planning_root_id: str,
        source_assistant_turn_id: str,
        request_contract: dict[str, Any],
        dimension_id: str,
        require_free_text: bool = False,
    ) -> bool:
        checkpoint_fingerprint = str(checkpoint.get("fingerprint") or "") if isinstance(checkpoint, dict) else ""
        expected_checkpoint_fingerprint = (
            cls._fingerprint({key: value for key, value in checkpoint.items() if key != "fingerprint"})
            if isinstance(checkpoint, dict)
            else ""
        )
        question = (
            checkpoint.get("question")
            if isinstance(checkpoint, dict) and isinstance(checkpoint.get("question"), dict)
            else {}
        )
        return bool(
            isinstance(checkpoint, dict)
            and checkpoint.get("schemaVersion") in cls.SUPPORTED_SCHEMA_VERSIONS
            and str(checkpoint.get("status") or "") == "awaiting_answer"
            and checkpoint_fingerprint
            and checkpoint_fingerprint == expected_checkpoint_fingerprint
            and str(checkpoint.get("checkpointId") or "") == str(checkpoint_id or "")
            and str(checkpoint.get("planningRootId") or "") == str(planning_root_id or "")
            and str(checkpoint.get("sourceAssistantTurnId") or "") == str(source_assistant_turn_id or "")
            and str(checkpoint.get("requestFingerprint") or "") == cls._fingerprint(request_contract)
            and str(checkpoint.get("nextQuestionDimensionId") or "") == str(dimension_id or "")
            and (not require_free_text or question.get("allowFreeText") is True)
        )

    @staticmethod
    def _options(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        options: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_semantics: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            option_id = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            semantic_value = item.get("semanticValue")
            if (
                not option_id
                or not label
                or not ClarificationCheckpointService.valid_semantic_patch(semantic_value)
                or option_id in seen_ids
            ):
                continue
            semantic_key = ClarificationCheckpointService._semantic_key(semantic_value)
            if semantic_key in seen_semantics:
                continue
            seen_ids.add(option_id)
            seen_semantics.add(semantic_key)
            options.append({"id": option_id, "label": label, "semanticValue": copy.deepcopy(semantic_value)})
        return options

    @classmethod
    def _valid_prior_checkpoint(
        cls,
        checkpoint: dict[str, Any],
        *,
        planning_root_id: str,
        request_contract: dict[str, Any],
        candidate_gap_summary: Optional[dict[str, Any]] = None,
    ) -> bool:
        fingerprint = str(checkpoint.get("fingerprint") or "")
        identity_valid = bool(
            checkpoint.get("schemaVersion") in cls.SUPPORTED_SCHEMA_VERSIONS
            and str(checkpoint.get("planningRootId") or "") == str(planning_root_id or "")
            and str(checkpoint.get("status") or "") in {"awaiting_answer", "awaiting_agent_resolution", "answered"}
            and str(checkpoint.get("sourceAssistantTurnId") or "")
            and fingerprint
            and fingerprint
            == cls._fingerprint({key: value for key, value in checkpoint.items() if key != "fingerprint"})
        )
        if not identity_valid:
            return False
        current_request_fingerprint = cls._fingerprint(request_contract)
        if str(checkpoint.get("requestFingerprint") or "") == current_request_fingerprint:
            return True
        return cls._valid_answered_contract_transition(
            checkpoint,
            request_contract=request_contract,
            candidate_gap_summary=candidate_gap_summary,
        )

    @classmethod
    def _valid_answered_contract_transition(
        cls,
        checkpoint: dict[str, Any],
        *,
        request_contract: dict[str, Any],
        candidate_gap_summary: Optional[dict[str, Any]],
    ) -> bool:
        """Validate the only legal request-fingerprint upgrade paths.

        A resolved semantic answer changes the canonical request contract, and
        a later grounded candidate gap may add one recovery dimension.  Both
        transitions are server-authored and carry exact checkpoint lineage;
        arbitrary changed contracts cannot reuse the prior answers/specs.
        """

        if str(checkpoint.get("status") or "") not in {
            "awaiting_answer",
            "awaiting_agent_resolution",
            "answered",
        }:
            return False
        if str(request_contract.get("clarificationCheckpointId") or "") != str(checkpoint.get("checkpointId") or ""):
            return False
        try:
            contract_version = int(request_contract.get("clarificationContractVersion"))
            checkpoint_version = int(checkpoint.get("contractVersion"))
        except (TypeError, ValueError):
            return False
        if contract_version != checkpoint_version:
            return False
        if cls._semantic_key(request_contract.get("clarificationAnswers")) != cls._semantic_key(
            checkpoint.get("resolvedAnswers") or []
        ):
            return False
        if cls._semantic_key(request_contract.get("experienceSpecs")) != cls._semantic_key(
            checkpoint.get("experienceSpecs") or []
        ):
            return False
        resolved_dimension_ids = {
            str(item.get("dimensionId") or "")
            for item in checkpoint.get("resolvedAnswers") or []
            if isinstance(item, dict) and str(item.get("dimensionId") or "")
        }
        contract_dimension_status = {
            str(item.get("dimensionId") or ""): str(item.get("status") or "")
            for item in request_contract.get("clarificationDimensions") or []
            if isinstance(item, dict) and str(item.get("dimensionId") or "")
        }
        if not resolved_dimension_ids or any(
            contract_dimension_status.get(dimension_id) != "resolved" for dimension_id in resolved_dimension_ids
        ):
            return False

        gap_fingerprint = str(request_contract.get("candidateGapFingerprint") or "")
        if gap_fingerprint:
            checkpoint_gap = (
                checkpoint.get("candidateGapSummary") if isinstance(checkpoint.get("candidateGapSummary"), dict) else {}
            )
            supplied_gap = candidate_gap_summary if isinstance(candidate_gap_summary, dict) else {}
            return bool(
                request_contract.get("clarificationDecisionSource") == "consumer_admission_candidate_gap"
                and gap_fingerprint
                == str(checkpoint_gap.get("fingerprint") or "")
                == str(supplied_gap.get("fingerprint") or "")
            )
        return bool(request_contract.get("clarificationDecisionSource") == "controller_semantic_choice")

    @staticmethod
    def _controller_identity_matches_checkpoint(
        controller_question: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> bool:
        """Accept server-bound lineage while rejecting any supplied mismatch.

        The controller owns the question semantics, not the durable checkpoint
        identity.  Requiring the model to echo every opaque identifier makes a
        valid follow-up question fail whenever the provider omits those fields.
        The caller already supplies and validates the persisted checkpoint, so
        omitted identity fields are safe; a value the controller does provide
        must still match exactly.
        """

        expected = {
            "checkpointId": str(checkpoint.get("checkpointId") or ""),
            "planningRootId": str(checkpoint.get("planningRootId") or ""),
            "requestFingerprint": str(checkpoint.get("requestFingerprint") or ""),
            "checkpointFingerprint": str(checkpoint.get("fingerprint") or ""),
        }
        return all(
            expected[field]
            and (controller_question.get(field) in (None, "") or str(controller_question.get(field)) == expected[field])
            for field in expected
        )

    @staticmethod
    def _unresolved_dimension_contract(
        request_contract: dict[str, Any],
        dimension_id: str,
    ) -> Optional[dict[str, Any]]:
        if not isinstance(request_contract, dict) or not str(dimension_id or "").strip():
            return None
        for item in request_contract.get("clarificationDimensions") or []:
            if (
                isinstance(item, dict)
                and str(item.get("dimensionId") or "") == str(dimension_id)
                and str(item.get("status") or "unresolved") == "unresolved"
            ):
                return copy.deepcopy(item)
        return None

    @classmethod
    def _ambiguity_state(
        cls,
        *,
        request_contract: dict[str, Any],
        prior_checkpoint: dict[str, Any],
        answers: list[dict[str, Any]],
        current_dimension_id: str,
        current_impact: str,
        current_options: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep one durable progress row for every contract dimension.

        The UI must report progress against the complete request contract, not
        merely the one question currently visible.  Answer semantics remain
        server-bound and are never inferred from a label.
        """

        prior_by_id = {
            str(item.get("dimensionId") or ""): item
            for item in prior_checkpoint.get("ambiguities") or []
            if isinstance(item, dict) and str(item.get("dimensionId") or "")
        }
        answer_by_id = {
            str(item.get("dimensionId") or ""): item
            for item in answers
            if isinstance(item, dict) and str(item.get("dimensionId") or "")
        }
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for dimension in request_contract.get("clarificationDimensions") or []:
            if not isinstance(dimension, dict):
                continue
            dimension_id = str(dimension.get("dimensionId") or "").strip()
            if not dimension_id or dimension_id in seen:
                continue
            seen.add(dimension_id)
            prior = prior_by_id.get(dimension_id, {})
            answer = answer_by_id.get(dimension_id, {})
            is_current = dimension_id == current_dimension_id
            resolved = str(dimension.get("status") or "unresolved") == "resolved" or bool(answer)
            result.append(
                {
                    "dimensionId": dimension_id,
                    "impact": (
                        current_impact if is_current else str(prior.get("impact") or dimension.get("impactCode") or "")
                    ),
                    "candidateScope": copy.deepcopy(
                        dimension.get("candidateScope") or prior.get("candidateScope") or {}
                    ),
                    "resolved": resolved,
                    "answerSource": (str(answer.get("source") or prior.get("answerSource") or "") or None),
                    "allowedSemanticValues": copy.deepcopy(
                        [item["semanticValue"] for item in current_options]
                        if is_current
                        else prior.get("allowedSemanticValues") or []
                    ),
                }
            )
        for dimension_id, prior in prior_by_id.items():
            if dimension_id in seen:
                continue
            answer = answer_by_id.get(dimension_id, {})
            result.append(
                {
                    **copy.deepcopy(prior),
                    "dimensionId": dimension_id,
                    "resolved": prior.get("resolved") is True or bool(answer),
                    "answerSource": str(answer.get("source") or prior.get("answerSource") or "") or None,
                }
            )
        return result

    @staticmethod
    def _valid_semantic_patch(value: Any) -> bool:
        if not isinstance(value, dict) or not value:
            return False
        allowed = {
            "intentType",
            "frequency",
            "occurrencePolicy",
            "allowedDayNumbers",
            "experienceFamilies",
            "accessPolicy",
            "distinctnessPolicy",
            "timeWindow",
            "detourTolerance",
            "adjacentLegConstraint",
            "spatialResolutionInput",
            "mobilityProfile",
            "evidenceFreshness",
            "confidence",
        }
        if not set(value).issubset(allowed):
            return False
        # A semantic patch must make a concrete, executable change.  Treating
        # an explicitly supplied null as "field absent" lets the Controller
        # mint a button that appears valid but cannot resolve the checkpoint.
        if any(item is None for item in value.values()):
            return False
        if any(
            marker in str(key).casefold()
            for key in value
            for marker in ("poi", "place", "name", "address", "location", "coordinate", "city")
        ):
            return False
        token_fields = (
            "intentType",
            "occurrencePolicy",
            "accessPolicy",
            "distinctnessPolicy",
        )
        token_pattern = re.compile(r"^[a-z][a-z0-9_.:-]{0,79}$")
        if any(
            value.get(field) is not None
            and (not isinstance(value.get(field), str) or token_pattern.fullmatch(str(value[field])) is None)
            for field in token_fields
        ):
            return False
        frequency = value.get("frequency")
        if frequency is not None and not (
            isinstance(frequency, int)
            and not isinstance(frequency, bool)
            and frequency > 0
            or isinstance(frequency, str)
            and token_pattern.fullmatch(frequency) is not None
        ):
            return False
        days = value.get("allowedDayNumbers")
        if days is not None and (
            not isinstance(days, list)
            or not days
            or any(not isinstance(day, int) or isinstance(day, bool) or day < 1 for day in days)
        ):
            return False
        families = value.get("experienceFamilies")
        if families is not None and (
            not isinstance(families, list)
            or not families
            or any(not isinstance(item, str) or token_pattern.fullmatch(item) is None for item in families)
        ):
            return False
        time_window = value.get("timeWindow")
        if time_window is not None:
            if not isinstance(time_window, dict) or set(time_window) != {
                "start",
                "end",
            }:
                return False
            for raw_time in time_window.values():
                if not isinstance(raw_time, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw_time) is None:
                    return False
        tolerance = value.get("detourTolerance")
        if tolerance is not None and (
            not isinstance(tolerance, dict)
            or set(tolerance) != {"maxGeneralizedCostDelta", "maxDetourRatio"}
            or not isinstance(tolerance.get("maxGeneralizedCostDelta"), (int, float))
            or isinstance(tolerance.get("maxGeneralizedCostDelta"), bool)
            or not isinstance(tolerance.get("maxDetourRatio"), (int, float))
            or isinstance(tolerance.get("maxDetourRatio"), bool)
            or float(tolerance["maxGeneralizedCostDelta"]) <= 0
            or float(tolerance["maxDetourRatio"]) < 0
            or not math.isfinite(float(tolerance["maxGeneralizedCostDelta"]))
            or not math.isfinite(float(tolerance["maxDetourRatio"]))
        ):
            return False
        adjacent = value.get("adjacentLegConstraint")
        if adjacent is not None and (
            not isinstance(adjacent, dict)
            or set(adjacent) != {"candidateSearchRadiusMeters", "maxProviderTravelMinutes"}
            or not isinstance(adjacent.get("candidateSearchRadiusMeters"), (int, float))
            or isinstance(adjacent.get("candidateSearchRadiusMeters"), bool)
            or not isinstance(adjacent.get("maxProviderTravelMinutes"), (int, float))
            or isinstance(adjacent.get("maxProviderTravelMinutes"), bool)
            or not math.isfinite(float(adjacent["candidateSearchRadiusMeters"]))
            or not math.isfinite(float(adjacent["maxProviderTravelMinutes"]))
            or not 100 <= float(adjacent["candidateSearchRadiusMeters"]) <= 50000
            or not 1 <= float(adjacent["maxProviderTravelMinutes"]) <= 480
        ):
            return False
        spatial_input = value.get("spatialResolutionInput")
        if spatial_input is not None:
            from src.services.spatial_preference_service import SpatialPreferenceService

            if not SpatialPreferenceService.valid_resolution_input(spatial_input):
                return False
        mobility = value.get("mobilityProfile")
        if mobility is not None and (
            not isinstance(mobility, dict)
            or set(mobility) != {"transportMode", "paceClass"}
            or str(mobility.get("transportMode") or "")
            not in {"transit", "public_transit", "driving", "walking", "bicycling"}
            or str(mobility.get("paceClass") or "") not in {"relaxed", "standard", "intensive"}
        ):
            return False
        freshness = value.get("evidenceFreshness")
        if freshness is not None:
            allowed_freshness = {
                "maxAgeHours",
                "requiredForControlledAccess",
                "requiredForPublicOutdoor",
                "allowExplicitNoClosure",
            }
            if (
                not isinstance(freshness, dict)
                or not freshness
                or not set(freshness).issubset(allowed_freshness)
                or not isinstance(freshness.get("maxAgeHours"), (int, float))
                or isinstance(freshness.get("maxAgeHours"), bool)
                or float(freshness["maxAgeHours"]) <= 0
                or not math.isfinite(float(freshness["maxAgeHours"]))
                or any(
                    key in freshness and not isinstance(freshness[key], bool)
                    for key in allowed_freshness - {"maxAgeHours"}
                )
            ):
                return False
        confidence = value.get("confidence")
        if confidence is not None and (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
            or not math.isfinite(float(confidence))
        ):
            return False
        return True

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _semantic_key(value: Any) -> str:
        if value in (None, ""):
            return ""
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def _experience_specs(
        cls,
        *,
        request_contract: dict[str, Any],
        answers: list[dict[str, Any]],
        ambiguities: list[dict[str, Any]],
        existing: Any,
    ) -> list[dict[str, Any]]:
        """Compile controller semantics into a POI-free, inspectable intent spec.

        This deliberately retains the controller's values verbatim where
        supplied; the only server derivation is the intent/day scope already
        present in the request contract.  It cannot introduce city landmarks.
        """
        specs = [copy.deepcopy(item) for item in existing or [] if isinstance(item, dict)]
        goals = [item for item in request_contract.get("requiredIntents") or [] if isinstance(item, dict)]
        by_intent = {str(item.get("intentType") or ""): item for item in goals}
        required_spec_fields = (
            "frequency",
            "allowedDayNumbers",
            "experienceFamilies",
            "accessPolicy",
            "distinctnessPolicy",
            "timeWindow",
            "detourTolerance",
            "evidenceFreshness",
            "confidence",
        )
        for answer in answers:
            dimension = str(answer.get("dimensionId") or "")
            if dimension.startswith("route_decision."):
                # Route-decision answers remain in resolvedAnswers and are
                # compiled by AgentService into the request-level route
                # contract.  They are not POI/experience intent semantics.
                continue
            value = answer.get("semanticValue")
            semantic = dict(value) if isinstance(value, dict) else {"value": value}
            intent = str(semantic.get("intentType") or dimension.split(".", 1)[0] or "").strip()
            if intent not in by_intent:
                intent = next(
                    (
                        known_intent
                        for known_intent in by_intent
                        if dimension == known_intent
                        or dimension.startswith(f"{known_intent}.")
                        or dimension.startswith(f"{known_intent}_")
                    ),
                    intent,
                )
            if not intent:
                continue
            goal = by_intent.get(intent, {})
            current = next((item for item in specs if item.get("intentType") == intent), {})
            unresolved_ambiguities = [
                str(item.get("dimensionId") or "")
                for item in ambiguities
                if isinstance(item, dict) and item.get("resolved") is not True
            ]
            frequency = semantic.get("frequency") or semantic.get("occurrencePolicy") or current.get("frequency")
            spec = {
                **current,
                "intentType": intent,
                "frequency": frequency,
                "allowedDayNumbers": list(
                    semantic.get("allowedDayNumbers")
                    or goal.get("allowedDayNumbers")
                    or current.get("allowedDayNumbers")
                    or []
                ),
                "experienceFamilies": list(
                    semantic.get("experienceFamilies") or current.get("experienceFamilies") or []
                ),
                "accessPolicy": semantic.get("accessPolicy") or current.get("accessPolicy"),
                "distinctnessPolicy": semantic.get("distinctnessPolicy") or current.get("distinctnessPolicy"),
                "timeWindow": semantic.get("timeWindow") or current.get("timeWindow"),
                "detourTolerance": semantic.get("detourTolerance") or current.get("detourTolerance"),
                "evidenceFreshness": semantic.get("evidenceFreshness") or current.get("evidenceFreshness"),
                "confidence": (
                    semantic.get("confidence") if semantic.get("confidence") is not None else current.get("confidence")
                ),
            }
            missing_fields = [
                f"{intent}.{field_name}"
                for field_name in required_spec_fields
                if spec.get(field_name) in (None, "", [])
                or (
                    isinstance(spec.get(field_name), str)
                    and str(spec.get(field_name) or "").casefold() in {"custom", "unknown", "unspecified", "pending"}
                )
            ]
            spec["unresolvedDimensions"] = list(dict.fromkeys([*unresolved_ambiguities, *missing_fields]))
            specs = [item for item in specs if item.get("intentType") != intent]
            specs.append(spec)
        return specs
