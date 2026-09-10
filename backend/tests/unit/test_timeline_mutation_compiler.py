import pytest

from src.services.timeline_mutation_compiler import TimelineMutationCompiler
from src.services.timeline_mutation_intent_service import TimelineMutationIntentExtractor
from src.services.timeline_mutation_models import TimelineMutationIntent, TimelineMutationResolution
from src.services.timeline_target_binder import TimelineTargetBinder

from timeline_mutation_test_support import append_second_day, campus_candidate, museum_candidate, open_db, seed_timeline


@pytest.mark.parametrize(
    ("text", "expected_op"),
    [
        ("第一天美术馆改为清华美术馆", "replace_segment_poi"),
        ("删除第一天的美术馆", "remove_segment"),
        ("把第一天美术馆改到 15:30", "replace_segment_start_time"),
        ("第一天美术馆停留改为 2 小时", "replace_segment_duration"),
        ("第一天午餐到美术馆改为公交地铁", "replace_transport_mode"),
    ],
)
def test_compiles_semantic_intents_to_existing_patch_operations(text, expected_op):
    with open_db() as connection:
        session, _, _ = seed_timeline(connection)
        intent = TimelineMutationIntentExtractor().extract(text, has_active_timeline=True)
        bound = TimelineTargetBinder(connection).bind(session.session_id, intent)
        resolution = (
            TimelineMutationResolution(status="unique_safe_candidate", selectedPoi=museum_candidate())
            if intent.operation == "replace_poi"
            else TimelineMutationResolution(status="not_required")
        )
        compiled = TimelineMutationCompiler().compile(bound, resolution)

    assert len(compiled.operations) == (2 if expected_op == "replace_segment_poi" else 1)
    assert compiled.operations[0].op == expected_op
    assert compiled.operations[0].segment_id == bound.target_segment_ids[0]
    if expected_op == "replace_segment_poi":
        assert compiled.operations[1].op == "replace_segment_duration"
        assert compiled.operations[1].duration_minutes == 90
        assert compiled.postcondition.expected_duration_minutes == 90
    assert compiled.postcondition.base_version_id == bound.base_version_id


def test_compiles_model_semantic_add_to_one_grounded_incremental_patch():
    intent = TimelineMutationIntent.model_validate(
        {
            "operation": "add_segment",
            "selector": {"dayNumber": 2, "intentType": "campus_visit"},
            "replacement": {"poiQuery": "985大学"},
            "sourceText": "Day 2 再安排一所符合要求的高校",
        }
    )
    with open_db() as connection:
        session, _, snapshot = seed_timeline(connection)
        _, snapshot = append_second_day(connection, session, snapshot)
        bound = TimelineTargetBinder(connection).bind(session.session_id, intent)
        compiled = TimelineMutationCompiler().compile(
            bound,
            TimelineMutationResolution(status="unique_safe_candidate", selectedPoi=campus_candidate()),
            before_snapshot=snapshot,
        )

    operation = compiled.operations[0]
    assert len(compiled.operations) == 1
    assert operation.op == "add_segment"
    assert operation.day_id == "day_mutation_2"
    assert operation.amap_poi.id == "B0PKU"
    assert operation.intent_type == "campus_visit"
    assert operation.duration_minutes == 120
    assert compiled.postcondition.target_segment_ids == []
    assert compiled.postcondition.target_day_id == "day_mutation_2"
