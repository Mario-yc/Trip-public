from __future__ import annotations

from src.services.portfolio_pending_slot_schedule_service import (
    PortfolioPendingSlotScheduleService,
)


def _segment(segment_id: str, start: str, end: str, *, locked: bool = False) -> dict:
    return {
        "id": segment_id,
        "startTime": start,
        "endTime": end,
        "kind": "visit",
        "semanticMetadata": {"userLocked": locked},
    }


def test_pending_meal_is_projected_into_real_gap_and_marked_route_pending():
    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    _segment("campus_am", "09:00", "11:20", locked=True),
                    _segment("campus_pm", "14:10", "16:10"),
                ],
            }
        ],
        "portfolioPendingSlots": [
            {
                "id": "pending_lunch",
                "briefId": "brief_a",
                "poolId": "pool_lunch",
                "planningSlotId": "slot_lunch",
                "dayNumber": 1,
                "timeWindow": "11:30-14:00",
                "startTime": "12:00",
                "endTime": "13:00",
                "durationMinutes": 60,
                "rawNeed": "当地特色午餐",
                "intentType": "meal",
                "kind": "meal",
            }
        ],
    }

    projected = PortfolioPendingSlotScheduleService().project(snapshot)
    slot = projected["portfolioPendingSlots"][0]

    assert slot["startTime"] == "12:00"
    assert slot["endTime"] == "13:00"
    assert slot["placementAfterSegmentId"] == "campus_am"
    assert slot["placementBeforeSegmentId"] == "campus_pm"
    assert slot["timingStatus"] == "awaiting_route_confirmation"
    assert slot["constraintSummary"].endswith("11:30–14:00")


def test_pending_service_does_not_infer_night_clock_time_from_daypart_label():
    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    _segment("late_visit", "16:00", "18:20"),
                    _segment("morning_visit", "09:00", "11:00"),
                ],
            }
        ],
        "portfolioPendingSlots": [
            {
                "id": "pending_night",
                "briefId": "brief_a",
                "poolId": "pool_night",
                "planningSlotId": "slot_night",
                "dayNumber": 1,
                "timeWindow": "night",
                "startTime": "14:00",
                "endTime": "15:30",
                "durationMinutes": 90,
                "rawNeed": "北京夜景",
                "intentType": "night_view",
                "kind": "visit",
            }
        ],
    }

    projected = PortfolioPendingSlotScheduleService().project(snapshot)
    slot = projected["portfolioPendingSlots"][0]

    assert slot["startTime"] == "14:00"
    assert slot["endTime"] == "15:30"
    assert slot["placementBeforeSegmentId"] == "late_visit"
    assert slot["timingStatus"] == "awaiting_route_confirmation"
    assert slot["timeWindow"] == "night"


def test_remaining_pending_slot_reflows_after_exact_slot_is_inserted():
    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    _segment("campus_am", "09:00", "11:00"),
                    _segment("selected_lunch", "11:30", "12:45"),
                    _segment("campus_pm", "13:00", "15:00"),
                ],
            }
        ],
        "portfolioPendingSlots": [
            {
                "id": "pending_walk",
                "briefId": "brief_a",
                "poolId": "pool_walk",
                "planningSlotId": "slot_walk",
                "dayNumber": 1,
                "timeWindow": "13:00-17:30",
                "startTime": "13:00",
                "endTime": "14:30",
                "durationMinutes": 90,
                "rawNeed": "街区漫步",
                "intentType": "area_walk",
                "kind": "visit",
            }
        ],
    }

    projected = PortfolioPendingSlotScheduleService().project(snapshot)
    slot = projected["portfolioPendingSlots"][0]

    assert slot["startTime"] == "15:00"
    assert slot["endTime"] == "16:30"
    assert slot["placementAfterSegmentId"] == "campus_pm"
    assert slot["timingStatus"] == "awaiting_route_confirmation"


def test_conflicting_pending_meals_keep_distinct_positions_inside_meal_window():
    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [_segment("fixed_lunch", "11:30", "14:00")],
            }
        ],
        "portfolioPendingSlots": [
            {
                "id": "pending_meal_one",
                "dayNumber": 1,
                "timeWindow": "11:30-14:00",
                "durationMinutes": 90,
                "intentType": "meal",
                "rawNeed": "当地特色午餐",
            },
            {
                "id": "pending_meal_two",
                "dayNumber": 1,
                "timeWindow": "11:30-14:00",
                "durationMinutes": 60,
                "intentType": "meal",
                "rawNeed": "当地特色午餐",
            },
        ],
    }

    projected = PortfolioPendingSlotScheduleService().project(snapshot)
    first, second = projected["portfolioPendingSlots"]

    assert (first["startTime"], first["endTime"]) == ("11:30", "13:00")
    assert (second["startTime"], second["endTime"]) == ("11:30", "12:30")
    assert first["timingStatus"] == "schedule_conflict_pending"
    assert second["timingStatus"] == "schedule_conflict_pending"


def test_contradictory_window_does_not_invent_a_night_time():
    projected = PortfolioPendingSlotScheduleService().project(
        {
            "days": [{"dayNumber": 1, "segments": []}],
            "portfolioPendingSlots": [
                {
                    "id": "pending_night",
                    "dayNumber": 1,
                    "timeWindow": "14:00-15:00",
                    "durationMinutes": 90,
                    "intentType": "night_view",
                    "rawNeed": "北京夜景",
                }
            ],
        }
    )

    slot = projected["portfolioPendingSlots"][0]
    assert slot["timingStatus"] == "time_pending"
    assert slot["timingLabel"] == "时间待定"
    assert "startTime" not in slot


def test_invalid_late_window_is_not_clamped_to_a_hardcoded_time():
    projected = PortfolioPendingSlotScheduleService().project(
        {
            "days": [{"dayNumber": 1, "segments": []}],
            "portfolioPendingSlots": [
                {
                    "id": "pending_night",
                    "dayNumber": 1,
                    "timeWindow": "23:00-23:30",
                    "durationMinutes": 90,
                    "intentType": "night_view",
                    "rawNeed": "北京夜景",
                }
            ],
        }
    )

    slot = projected["portfolioPendingSlots"][0]
    assert slot["timingStatus"] == "time_pending"
    assert slot["timingLabel"] == "时间待定"
    assert "startTime" not in slot


def test_pending_slot_follows_non_default_compiled_windows_without_domain_inference():
    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    _segment("before", "08:20", "10:10"),
                    _segment("middle", "11:40", "18:50"),
                    _segment("after", "20:50", "21:30"),
                ],
            }
        ],
        "portfolioPendingSlots": [
            {
                "id": "pending_campus",
                "dayNumber": 1,
                "timeWindow": "10:20-11:35",
                "durationMinutes": 75,
                "intentType": "campus_visit",
            },
            {
                "id": "pending_night",
                "dayNumber": 1,
                "timeWindow": "19:10-20:40",
                "durationMinutes": 90,
                "intentType": "night_view",
            },
        ],
    }

    first, second = PortfolioPendingSlotScheduleService().project(snapshot)[
        "portfolioPendingSlots"
    ]

    assert (first["startTime"], first["endTime"]) == ("10:20", "11:35")
    assert (second["startTime"], second["endTime"]) == ("19:10", "20:40")
    assert first["placementBeforeSegmentId"] == "middle"
    assert second["placementAfterSegmentId"] == "middle"


def test_pending_slot_without_server_time_constraint_remains_time_pending():
    projected = PortfolioPendingSlotScheduleService().project(
        {
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        _segment("morning", "10:00", "11:00"),
                        _segment("afternoon", "15:00", "16:00"),
                    ],
                }
            ],
            "portfolioPendingSlots": [
                {
                    "id": "pending_unknown",
                    "dayNumber": 1,
                    "planningSlotId": "slot_unknown",
                    "intentType": "area_walk",
                }
            ],
        }
    )

    slot = projected["portfolioPendingSlots"][0]
    assert "startTime" not in slot
    assert "endTime" not in slot
    assert "durationMinutes" not in slot
    assert slot["timingStatus"] == "time_pending"
    assert slot["timingLabel"] == "时间待定"


def test_pending_slot_normalizes_missing_time_window_from_exact_schedule():
    projected = PortfolioPendingSlotScheduleService().project(
        {
            "days": [{"dayNumber": 1, "segments": []}],
            "portfolioPendingSlots": [
                {
                    "id": "pending_dynamic",
                    "dayNumber": 1,
                    "planningSlotId": "slot_dynamic",
                    "startTime": "10:20",
                    "endTime": "11:35",
                    "durationMinutes": 75,
                    "intentType": "campus_visit",
                }
            ],
        }
    )

    slot = projected["portfolioPendingSlots"][0]
    assert slot["timeWindow"] == "10:20-11:35"
    assert slot["startTime"] == "10:20"
    assert slot["endTime"] == "11:35"
