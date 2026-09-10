from src.services.timeline_mutation_intent_service import TimelineMutationIntentExtractor
import pytest
from pydantic import ValidationError

from src.services.timeline_mutation_models import TimelineMutationIntent
from src.services.timeline_target_binder import TimelineTargetBinder

from timeline_mutation_test_support import append_second_day, open_db, seed_timeline


def intent(text="第一天美术馆改为清华美术馆"):
    return TimelineMutationIntentExtractor().extract(text, has_active_timeline=True)


def test_binds_baseline_museum_from_active_snapshot():
    with open_db() as connection:
        session, version, _ = seed_timeline(connection)
        bound = TimelineTargetBinder(connection).bind(session.session_id, intent())

    assert bound.binding_status == "unique"
    assert bound.base_version_id == version.id
    assert bound.target_segment_ids == ["seg_831b98d1cb6e"]
    assert "intentType_exact" in bound.binding_evidence


def test_ambiguous_and_not_found_fail_closed():
    with open_db() as connection:
        session, _, _ = seed_timeline(connection, duplicate_museum=True)
        ambiguous = TimelineTargetBinder(connection).bind(
            session.session_id,
            TimelineMutationIntentExtractor().extract("美术馆改为清华美术馆", has_active_timeline=True),
        )
        missing_intent = TimelineMutationIntentExtractor().extract("第一天颐和园改为圆明园", has_active_timeline=True)
        missing = TimelineTargetBinder(connection).bind(session.session_id, missing_intent)

    assert ambiguous.binding_status == "ambiguous"
    assert len(ambiguous.target_segment_ids) == 2
    assert missing.binding_status == "target_not_found"


def test_transport_binds_the_adjacent_from_segment():
    with open_db() as connection:
        session, _, _ = seed_timeline(connection)
        bound = TimelineTargetBinder(connection).bind(
            session.session_id,
            TimelineMutationIntentExtractor().extract(
                "第一天午餐到美术馆改为公交地铁", has_active_timeline=True
            ),
        )

    assert bound.binding_status == "unique"
    assert bound.target_segment_ids == ["seg_mid"]


def test_add_segment_binds_only_the_semantic_target_day_without_rebinding_existing_nodes():
    mutation = TimelineMutationIntent.model_validate(
        {
            "operation": "add_segment",
            "selector": {"dayNumber": 2, "intentType": "campus_visit"},
            "replacement": {"poiQuery": "985大学"},
            "sourceText": "在第二日追加一所符合要求的高校",
        }
    )
    with open_db() as connection:
        session, _, snapshot = seed_timeline(connection)
        version, _ = append_second_day(connection, session, snapshot)
        bound = TimelineTargetBinder(connection).bind(session.session_id, mutation)

    assert bound.binding_status == "unique"
    assert bound.base_version_id == version.id
    assert bound.target_day_id == "day_mutation_2"
    assert bound.target_segment_ids == []
    assert bound.target_descriptors == []


def test_add_segment_day_number_rejects_boolean_payload():
    with pytest.raises(ValidationError):
        TimelineMutationIntent.model_validate(
            {
                "operation": "add_segment",
                "selector": {"dayNumber": True, "intentType": "campus_visit"},
                "replacement": {"poiQuery": "university"},
            }
        )


def test_missing_semantic_metadata_does_not_infer_goal_fields_from_notes():
    descriptors = TimelineTargetBinder.descriptors(
        {
            "days": [
                {
                    "id": "day_1",
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "seg_legacy",
                            "kind": "visit",
                            "startTime": "09:00",
                            "endTime": "10:00",
                            "notes": "goalId=goal_museum；intentType=museum；required=true；routeAnchor=true",
                            "poi": {
                                "name": "旧草案地点",
                                "type": "风景名胜",
                                "category": "scenic",
                                "routeable": False,
                            },
                        }
                    ],
                }
            ]
        }
    )

    descriptor = descriptors[0]
    assert descriptor.intent_type is None
    assert descriptor.raw_need == "旧草案地点"
    assert descriptor.required is False
    assert descriptor.route_anchor is False
