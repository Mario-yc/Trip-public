from src.services.timeline_mutation_intent_service import TimelineMutationIntentExtractor
from src.services.agent_service import AgentService


def test_extracts_baseline_replace_without_internal_ids():
    intent = TimelineMutationIntentExtractor().extract("第一天美术馆改为清华美术馆", has_active_timeline=True)

    assert intent is not None
    assert intent.operation == "replace_poi"
    assert intent.selector.day_number == 1
    assert intent.selector.intent_type == "museum"
    assert intent.selector.current_text == "美术馆"
    assert intent.replacement.poi_query == "清华美术馆"
    payload = intent.model_dump(by_alias=True)
    assert not {"baseVersionId", "segmentId", "dayId", "amapPoiId", "patchId"} & set(str(payload))


def test_extracts_remove_time_duration_and_transport_with_same_contract():
    extractor = TimelineMutationIntentExtractor()

    assert extractor.extract("删除第一天的美术馆", has_active_timeline=True).operation == "remove_segment"
    start = extractor.extract("把第一天美术馆改到 15:30", has_active_timeline=True)
    assert (start.operation, start.replacement.start_time) == ("set_start_time", "15:30")
    duration = extractor.extract("第一天美术馆停留改为 2 小时", has_active_timeline=True)
    assert (duration.operation, duration.replacement.duration_minutes) == ("set_duration", 120)
    transport = extractor.extract("第一天清华大学到美术馆改为公交地铁", has_active_timeline=True)
    assert (transport.operation, transport.replacement.transport_mode) == ("set_transport_mode", "transit")


def test_rejects_generic_grounding_request_and_timeline_without_active_version():
    extractor = TimelineMutationIntentExtractor()

    assert extractor.extract("第二天夜景不是具体地点，改为真实地点", has_active_timeline=True) is None
    assert extractor.extract("第一天美术馆改为清华美术馆", has_active_timeline=False) is None


def test_controller_semantic_payload_uses_same_id_free_contract():
    context = {
        "agentDecision": {
            "primaryAction": "patch_itinerary",
            "actionDirective": {
                "type": "patch_itinerary",
                "mutationIntent": {
                    "operation": "replace_poi",
                    "selector": {"dayNumber": 1, "intentType": "museum", "currentText": "美术馆"},
                    "replacement": {"poiQuery": "清华美术馆"},
                },
            },
        }
    }

    intent = AgentService._controller_timeline_mutation_intent(context, "请替换")

    assert intent is not None
    assert intent.source == "model_semantic_extractor"
    assert intent.source_text == "请替换"
    assert intent.selector.day_number == 1
