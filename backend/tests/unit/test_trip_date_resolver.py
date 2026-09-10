from datetime import date

import pytest

from src.services.trip_date_resolver import TripDateResolver


def test_relative_day_expression_resolves_against_injected_clock():
    resolver = TripDateResolver(clock=lambda: date(2026, 7, 14))

    assert resolver.resolve("今天出发").start_date == "2026-07-14"
    assert resolver.resolve("明天出发").start_date == "2026-07-15"
    assert resolver.resolve("后天出发").start_date == "2026-07-16"
    assert resolver.resolve("明天出发").date_precision == "relative_date"


def test_national_day_resolves_to_current_year_before_holiday_window():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve("国庆节去北京")

    payload = resolved.to_camel_dict()
    assert payload["status"] == "resolved"
    assert payload["startDate"] == "2026-10-01"
    assert payload["endDate"] == "2026-10-07"
    assert payload["holidayInferred"] is True
    assert payload["datePrecision"] == "holiday_inferred"
    assert payload["exactOfficialHolidayCalendar"] is False
    assert payload["weatherForecastSupported"] is False
    assert payload["reason"] == "date_outside_supported_forecast_window"


def test_national_day_without_year_rolls_to_next_year_after_holiday():
    resolved = TripDateResolver(clock=lambda: date(2026, 10, 8)).resolve("十一黄金周安排旅行")

    assert resolved.start_date == "2027-10-01"
    assert resolved.end_date == "2027-10-07"


def test_month_day_without_year_rolls_to_next_year_when_date_already_passed():
    resolved = TripDateResolver(clock=lambda: date(2026, 10, 8)).resolve("10月1日去北京")

    assert resolved.start_date == "2027-10-01"
    assert resolved.end_date == "2027-10-01"


def test_month_day_range_without_year_rolls_to_next_year_when_start_already_passed():
    resolved = TripDateResolver(clock=lambda: date(2026, 10, 8)).resolve("10月1日-10月3日去北京")

    assert resolved.start_date == "2027-10-01"
    assert resolved.end_date == "2027-10-03"


def test_month_day_to_day_range_preserves_two_day_trip():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve("10月1日到2日，2天")

    assert resolved.start_date == "2026-10-01"
    assert resolved.end_date == "2026-10-02"
    assert resolved.dates == ["2026-10-01", "2026-10-02"]


def test_identical_explicit_range_repeated_across_root_and_continuation_is_not_ambiguous():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(
        "10月1日到2日，2天。\n继续执行：10月1日到2日，2天。"
    )

    assert resolved.status == "resolved"
    assert resolved.dates == ["2026-10-01", "2026-10-02"]


def test_conflicting_explicit_ranges_across_root_and_continuation_fail_closed():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(
        "10月1日到2日。\n继续执行：10月2日到3日。"
    )

    assert resolved.status == "unresolved"
    assert resolved.reason == "ambiguous_multiple_explicit_date_groups"


def test_explicit_comma_separated_dates_preserve_both_trip_days():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(
        "今年国庆，10月1日，10月2日两天，去北京旅行"
    )

    assert resolved.start_date == "2026-10-01"
    assert resolved.end_date == "2026-10-02"
    assert resolved.dates == ["2026-10-01", "2026-10-02"]
    assert resolved.date_precision == "date_range"


def test_identical_explicit_date_list_repeated_in_effective_message_is_not_ambiguous():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(
        "十月一日、十月二日。\n确认并继续：十月一日、十月二日。"
    )

    assert resolved.status == "resolved"
    assert resolved.dates == ["2026-10-01", "2026-10-02"]


def test_explicit_three_date_list_does_not_silently_drop_the_third_day():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(
        "今年国庆，10月1日、10月2日、10月3日三天去北京"
    )

    assert resolved.start_date == "2026-10-01"
    assert resolved.end_date == "2026-10-03"
    assert resolved.dates == ["2026-10-01", "2026-10-02", "2026-10-03"]


@pytest.mark.parametrize("connector", ["以及", "与", "还有"])
def test_explicit_three_date_list_accepts_common_final_connectors(connector: str):
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(
        f"今年国庆，10月1日、10月2日{connector}10月3日三天去北京"
    )

    assert resolved.dates == ["2026-10-01", "2026-10-02", "2026-10-03"]


def test_national_day_first_two_days_does_not_expand_to_full_holiday_week():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(
        "今年国庆头两天去北京"
    )

    assert resolved.start_date == "2026-10-01"
    assert resolved.end_date == "2026-10-02"
    assert resolved.dates == ["2026-10-01", "2026-10-02"]
    assert resolved.date_precision == "holiday_duration"


def test_bare_national_day_previous_two_days_fails_closed_as_ambiguous():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(
        "今年国庆前两天去北京"
    )

    assert resolved.status == "unresolved"
    assert resolved.dates == []
    assert resolved.reason == "ambiguous_national_day_relative_duration"


def test_explicit_chinese_date_range_resolves_dates():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve("2026年10月1日-10月3日")

    assert resolved.date_precision == "date_range"
    assert resolved.dates == ["2026-10-01", "2026-10-02", "2026-10-03"]


def test_explicit_chinese_date_range_allows_natural_spacing():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(
        "我准备在 2026 年 10 月 1 日到 10 月 3 日出行"
    )

    assert resolved.date_precision == "date_range"
    assert resolved.dates == ["2026-10-01", "2026-10-02", "2026-10-03"]


def test_original_chinese_two_day_request_prefers_explicit_dates_over_holiday():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve("今年国庆十月一日、十月二日去北京玩两天")

    assert resolved.status == "resolved"
    assert resolved.dates == ["2026-10-01", "2026-10-02"]
    assert resolved.holiday_inferred is False
    assert resolved.date_precision == "date_range"


@pytest.mark.parametrize(
    ("expression", "expected_precision"),
    [
        ("十月一日、十月二日", "date_range"),
        ("十月一日至十月二日", "date_range"),
        ("10月1日、十月二日", "date_range"),
        ("10月一日到十月2日", "date_range"),
        ("十月一日", "exact_date"),
        ("10月一日", "exact_date"),
        ("十月1日", "exact_date"),
    ],
)
def test_chinese_arabic_and_mixed_month_day_tokens_share_one_parser(
    expression: str,
    expected_precision: str,
):
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(expression)

    assert resolved.status == "resolved"
    assert resolved.start_date == "2026-10-01"
    assert resolved.end_date == ("2026-10-02" if expected_precision == "date_range" else "2026-10-01")
    assert resolved.date_precision == expected_precision


@pytest.mark.parametrize(
    "expression",
    [
        "十月一日，十月二日",
        "十月一日、十月二日",
        "十月一日和十月二日",
        "十月一日以及十月二日",
        "十月一日与十月二日",
    ],
)
def test_chinese_explicit_date_list_accepts_supported_separators(expression: str):
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(expression)

    assert resolved.dates == ["2026-10-01", "2026-10-02"]


@pytest.mark.parametrize(
    "expression",
    [
        "10月1日至10月3日，玩两天",
        "十月一日、十月二日、十月三日，两天行程",
        "十月一日至十月三日，两日行程",
        "2026年十月一日至2026年十月三日，安排两日",
    ],
)
def test_explicit_date_count_conflicting_with_duration_is_typed_unresolved(expression: str):
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(expression)

    assert resolved.status == "unresolved"
    assert resolved.dates == []
    assert resolved.reason == "date_duration_conflict"


def test_national_day_two_days_without_specific_dates_requires_clarification():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve("今年国庆玩两天，具体日期还没定")

    assert resolved.status == "unresolved"
    assert resolved.dates == []
    assert resolved.reason == "holiday_dates_unspecified"


@pytest.mark.parametrize(
    "message",
    [
        "十月三十二日去北京",
        "十三月一日去北京",
        "10月100日去北京",
        "10月1日、十月三日去北京",
        "明天确认，但十月三十二日这个日期需要更正",
    ],
)
def test_current_explicit_date_error_does_not_fall_back_to_old_context(message: str):
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(
        message,
        {
            "resolvedTripDates": {
                "status": "resolved",
                "startDate": "2026-09-01",
                "endDate": "2026-09-02",
                "dates": ["2026-09-01", "2026-09-02"],
            },
            "travelDateRange": {"startDate": "2026-09-01", "endDate": "2026-09-02"},
        },
    )

    assert resolved.status == "unresolved"
    assert resolved.dates == []
    assert resolved.start_date is None
    assert resolved.reason in {
        "invalid_calendar_date",
        "non_contiguous_explicit_date_list",
    }


def test_current_duration_edit_does_not_reuse_old_timeline_dates() -> None:
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(
        "改成三天，增加一个公园",
        {
            "timelineContext": {
                "days": [
                    {"dayNumber": 1, "date": "2026-10-01"},
                    {"dayNumber": 2, "date": "2026-10-02"},
                ]
            }
        },
        source="latestUserMessage",
    )

    assert resolved.status == "unresolved"
    assert resolved.source == "latestUserMessage"
    assert resolved.dates == []
    assert resolved.reason == "duration_without_dates"


def test_ordinal_itinerary_day_reference_is_not_a_duration_edit() -> None:
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(
        "把第一天上午第一站开始时间改为08:00，其他安排不变",
        {
            "timelineContext": {
                "days": [
                    {"dayNumber": 1, "date": "2026-10-01"},
                    {"dayNumber": 2, "date": "2026-10-02"},
                ]
            }
        },
        source="latestUserMessage",
    )

    assert resolved.status == "resolved"
    assert resolved.source == "context.timelineContext"
    assert resolved.dates == ["2026-10-01", "2026-10-02"]


def test_date_range_matching_duration_remains_resolved():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve("十月一日至十月二日，玩两天")

    assert resolved.status == "resolved"
    assert resolved.dates == ["2026-10-01", "2026-10-02"]


@pytest.mark.parametrize(
    ("expression", "expected_date"),
    [
        ("一月一日出发", "2027-01-01"),
        ("2026年十二月三十一日出发", "2026-12-31"),
        ("2028年二月二十九日出发", "2028-02-29"),
    ],
)
def test_supported_chinese_month_day_boundaries(expression: str, expected_date: str):
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve(expression)

    assert resolved.status == "resolved"
    assert resolved.dates == [expected_date]


def test_calendar_day_followed_by_travel_verb_is_not_misread_as_trip_duration():
    resolved = TripDateResolver(clock=lambda: date(2026, 7, 1)).resolve("十月二日游览北京")

    assert resolved.status == "resolved"
    assert resolved.dates == ["2026-10-02"]
