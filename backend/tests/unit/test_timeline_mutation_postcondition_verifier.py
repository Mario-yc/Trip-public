from copy import deepcopy

from src.services.timeline_mutation_models import MutationPostconditionSpec
from src.services.timeline_mutation_postcondition_verifier import TimelineMutationPostconditionVerifier

from timeline_mutation_test_support import open_db, seed_timeline


def test_replace_poi_postcondition_accepts_target_and_derived_schedule_only():
    with open_db() as connection:
        _, _, before = seed_timeline(connection)
    after = deepcopy(before)
    target = find(after, "seg_831b98d1cb6e")
    target["poi"].update({"id": "poi_new", "amapId": "B0MUSEUM", "name": "清华大学艺术博物馆", "type": "科教文化服务;博物馆"})
    target["semanticMetadata"].update({"intentType": "museum", "groundingStatus": "selected"})
    find(after, "seg_after")["startTime"] = "20:00"
    find(after, "seg_after")["endTime"] = "21:30"
    spec = MutationPostconditionSpec(
        operation="replace_poi",
        baseVersionId="ver_base",
        targetSegmentIds=["seg_831b98d1cb6e"],
        expectedPoiAmapId="B0MUSEUM",
        expectedPoiName="清华大学艺术博物馆",
        expectedIntentType="museum",
        expectedDurationMinutes=90,
        targetDayId=before["days"][0]["id"],
        allowedDerivedSegmentIds=["seg_after"],
        preserve=["other_segment_pois"],
    )

    report = TimelineMutationPostconditionVerifier().verify(before, after, spec, version_delta=1)

    assert report.passed is True
    assert report.diff.direct_changed_segment_ids == ["seg_831b98d1cb6e"]
    assert report.diff.derived_changed_segment_ids == ["seg_after"]


def test_replace_poi_postcondition_rejects_target_duration_change():
    with open_db() as connection:
        _, _, before = seed_timeline(connection)
    after = deepcopy(before)
    target = find(after, "seg_831b98d1cb6e")
    target["poi"].update(
        {
            "id": "poi_new",
            "amapId": "B0MUSEUM",
            "name": "清华大学艺术博物馆",
            "type": "科教文化服务;博物馆",
        }
    )
    target["semanticMetadata"].update({"intentType": "museum", "groundingStatus": "selected"})
    target["endTime"] = "18:15"
    spec = MutationPostconditionSpec(
        operation="replace_poi",
        baseVersionId="ver_base",
        targetSegmentIds=["seg_831b98d1cb6e"],
        expectedPoiAmapId="B0MUSEUM",
        expectedPoiName="清华大学艺术博物馆",
        expectedIntentType="museum",
        expectedDurationMinutes=90,
    )

    report = TimelineMutationPostconditionVerifier().verify(before, after, spec, version_delta=1)

    assert report.passed is False
    assert "replace_poi changed target duration" in report.errors


def test_postcondition_rejects_locked_time_and_other_day_schedule_changes():
    with open_db() as connection:
        _, _, before = seed_timeline(connection)
    before["days"][0]["segments"][-1].setdefault("estimateMetadata", {}).setdefault("duration", {})[
        "userLocked"
    ] = True
    other_day_segment = deepcopy(before["days"][0]["segments"][-1])
    other_day_segment.update({"id": "seg_other_day", "dayId": "day_other", "startTime": "09:00", "endTime": "10:30"})
    before["days"].append({"id": "day_other", "dayNumber": 2, "segments": [other_day_segment]})
    after = deepcopy(before)
    target = find(after, "seg_831b98d1cb6e")
    target["poi"].update(
        {"id": "poi_new", "amapId": "B0MUSEUM", "name": "清华大学艺术博物馆", "type": "科教文化服务;博物馆"}
    )
    target["semanticMetadata"].update({"intentType": "museum", "groundingStatus": "selected"})
    find(after, "seg_after").update({"startTime": "20:00", "endTime": "21:30"})
    find(after, "seg_other_day").update({"startTime": "10:00", "endTime": "11:30"})
    spec = MutationPostconditionSpec(
        operation="replace_poi",
        baseVersionId="ver_base",
        targetSegmentIds=["seg_831b98d1cb6e"],
        targetDayId=before["days"][0]["id"],
        allowedDerivedSegmentIds=["seg_after"],
        expectedPoiAmapId="B0MUSEUM",
        expectedPoiName="清华大学艺术博物馆",
        expectedIntentType="museum",
        preserve=["other_segments", "other_days", "user_locked_times"],
    )

    report = TimelineMutationPostconditionVerifier().verify(before, after, spec, version_delta=1)

    assert report.passed is False
    assert set(report.diff.unexpected_changed_segment_ids) == {"seg_after", "seg_other_day"}
    assert any("user-locked" in error for error in report.errors)
    assert any("outside target day" in error for error in report.errors)


def test_postcondition_rejects_unlisted_pre_target_estimate_metadata_change():
    with open_db() as connection:
        _, _, before = seed_timeline(connection)
    after = deepcopy(before)
    target = find(after, "seg_831b98d1cb6e")
    target["poi"].update(
        {"id": "poi_new", "amapId": "B0MUSEUM", "name": "清华大学艺术博物馆", "type": "科教文化服务;博物馆"}
    )
    target["semanticMetadata"].update({"intentType": "museum", "groundingStatus": "selected"})
    find(after, "seg_before").setdefault("estimateMetadata", {}).setdefault("duration", {})["userLocked"] = True
    spec = MutationPostconditionSpec(
        operation="replace_poi",
        baseVersionId="ver_base",
        targetSegmentIds=["seg_831b98d1cb6e"],
        targetDayId=before["days"][0]["id"],
        allowedDerivedSegmentIds=["seg_after"],
        expectedPoiAmapId="B0MUSEUM",
        expectedPoiName="清华大学艺术博物馆",
        expectedIntentType="museum",
        preserve=["other_segments", "other_days", "user_locked_times"],
    )

    report = TimelineMutationPostconditionVerifier().verify(before, after, spec, version_delta=1)

    assert report.passed is False
    assert report.diff.unexpected_changed_segment_ids == ["seg_before"]


def test_postcondition_rejects_fabricated_schedule_metadata_status():
    with open_db() as connection:
        _, _, before = seed_timeline(connection)
    after = deepcopy(before)
    target = find(after, "seg_831b98d1cb6e")
    target["poi"].update(
        {"id": "poi_new", "amapId": "B0MUSEUM", "name": "清华大学艺术博物馆", "type": "科教文化服务;博物馆"}
    )
    target["semanticMetadata"].update({"intentType": "museum", "groundingStatus": "selected"})
    find(after, "seg_before").setdefault("estimateMetadata", {})["schedule"] = {
        "status": "fabricated",
        "routeBufferMinutes": 0,
    }
    spec = MutationPostconditionSpec(
        operation="replace_poi",
        baseVersionId="ver_base",
        targetSegmentIds=["seg_831b98d1cb6e"],
        targetDayId=before["days"][0]["id"],
        allowedDerivedSegmentIds=["seg_after"],
        allowedScheduleMetadataSegmentIds=["seg_before", "seg_mid", "seg_after"],
        expectedPoiAmapId="B0MUSEUM",
        expectedPoiName="清华大学艺术博物馆",
        expectedIntentType="museum",
        preserve=["other_segments", "other_days", "user_locked_times"],
    )

    report = TimelineMutationPostconditionVerifier().verify(before, after, spec, version_delta=1)

    assert report.passed is False
    assert report.diff.unexpected_changed_segment_ids == ["seg_before"]


def test_postcondition_rejects_allowed_time_cascade_with_fabricated_schedule_metadata():
    with open_db() as connection:
        _, _, before = seed_timeline(connection)
    after = deepcopy(before)
    target = find(after, "seg_831b98d1cb6e")
    target["poi"].update(
        {"id": "poi_new", "amapId": "B0MUSEUM", "name": "清华大学艺术博物馆", "type": "科教文化服务;博物馆"}
    )
    target["semanticMetadata"].update({"intentType": "museum", "groundingStatus": "selected"})
    derived = find(after, "seg_after")
    derived.update({"startTime": "20:00", "endTime": "21:30"})
    derived.setdefault("estimateMetadata", {})["schedule"] = {
        "status": "fabricated",
        "routeBufferMinutes": 999,
    }
    spec = MutationPostconditionSpec(
        operation="replace_poi",
        baseVersionId="ver_base",
        targetSegmentIds=["seg_831b98d1cb6e"],
        targetDayId=before["days"][0]["id"],
        allowedDerivedSegmentIds=["seg_after"],
        allowedScheduleMetadataSegmentIds=["seg_before", "seg_mid", "seg_after"],
        expectedPoiAmapId="B0MUSEUM",
        expectedPoiName="清华大学艺术博物馆",
        expectedIntentType="museum",
        preserve=["other_segments", "other_days", "user_locked_times"],
    )

    report = TimelineMutationPostconditionVerifier().verify(before, after, spec, version_delta=1)

    assert report.passed is False
    assert report.diff.unexpected_changed_segment_ids == ["seg_after"]


def test_postcondition_rejects_unrelated_poi_change_and_wrong_version_delta():
    with open_db() as connection:
        _, _, before = seed_timeline(connection)
    after = deepcopy(before)
    target = find(after, "seg_831b98d1cb6e")
    target["poi"].update({"amapId": "B0MUSEUM", "name": "清华大学艺术博物馆", "type": "科教文化服务;博物馆"})
    find(after, "seg_before")["poi"]["name"] = "被错误修改"
    spec = MutationPostconditionSpec(
        operation="replace_poi",
        baseVersionId="ver_base",
        targetSegmentIds=["seg_831b98d1cb6e"],
        expectedPoiAmapId="B0MUSEUM",
        expectedIntentType="museum",
    )

    report = TimelineMutationPostconditionVerifier().verify(before, after, spec, version_delta=0)

    assert report.passed is False
    assert "seg_before" in report.diff.unexpected_changed_segment_ids
    assert any("version delta" in error for error in report.errors)


def test_replace_postcondition_rejects_wrong_canonical_name_and_non_poi_field_change():
    with open_db() as connection:
        _, _, before = seed_timeline(connection)
    after = deepcopy(before)
    target = find(after, "seg_831b98d1cb6e")
    target["poi"].update({"amapId": "B0MUSEUM", "name": "另一个博物馆", "type": "科教文化服务;博物馆"})
    target["transportMode"] = "driving"
    target["semanticMetadata"].update({"intentType": "museum", "groundingStatus": "selected"})
    spec = MutationPostconditionSpec(
        operation="replace_poi",
        baseVersionId="ver_base",
        targetSegmentIds=["seg_831b98d1cb6e"],
        expectedPoiAmapId="B0MUSEUM",
        expectedPoiName="清华大学艺术博物馆",
        expectedIntentType="museum",
    )

    report = TimelineMutationPostconditionVerifier().verify(before, after, spec, version_delta=1)

    assert report.passed is False
    assert any("canonical name" in error for error in report.errors)
    assert any("transportMode" in error for error in report.errors)


def test_remove_time_duration_and_transport_postconditions():
    with open_db() as connection:
        _, _, before = seed_timeline(connection)
    verifier = TimelineMutationPostconditionVerifier()

    removed = deepcopy(before)
    removed["days"][0]["segments"] = [item for item in removed["days"][0]["segments"] if item["id"] != "seg_831b98d1cb6e"]
    assert verifier.verify(before, removed, MutationPostconditionSpec(operation="remove_segment", baseVersionId="v", targetSegmentIds=["seg_831b98d1cb6e"]), version_delta=1).passed

    timed = deepcopy(before)
    find(timed, "seg_831b98d1cb6e").update({"startTime": "15:30", "endTime": "17:00"})
    assert verifier.verify(before, timed, MutationPostconditionSpec(operation="set_start_time", baseVersionId="v", targetSegmentIds=["seg_831b98d1cb6e"], expectedStartTime="15:30"), version_delta=1).passed

    duration = deepcopy(before)
    find(duration, "seg_831b98d1cb6e")["endTime"] = "18:15"
    assert verifier.verify(before, duration, MutationPostconditionSpec(operation="set_duration", baseVersionId="v", targetSegmentIds=["seg_831b98d1cb6e"], expectedDurationMinutes=120), version_delta=1).passed

    transport = deepcopy(before)
    find(transport, "seg_mid")["transportMode"] = "transit"
    assert verifier.verify(before, transport, MutationPostconditionSpec(operation="set_transport_mode", baseVersionId="v", targetSegmentIds=["seg_mid"], expectedTransportMode="transit"), version_delta=1).passed


def find(snapshot, segment_id):
    return next(segment for day in snapshot["days"] for segment in day["segments"] if segment["id"] == segment_id)
