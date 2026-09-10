from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional


Clock = Callable[[], date]


@dataclass(frozen=True)
class ResolvedTripDates:
    status: str
    start_date: Optional[str]
    end_date: Optional[str]
    dates: list[str]
    source: str
    date_precision: str
    holiday_name: Optional[str] = None
    holiday_inferred: bool = False
    exact_official_holiday_calendar: bool = False
    weather_forecast_supported: Optional[bool] = None
    reason: Optional[str] = None
    weather_status: str = "not_requested"
    forecast_available_from: Optional[str] = None
    weather_next_action: str = "provide_trip_dates"

    def to_camel_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            "status": data["status"],
            "startDate": data["start_date"],
            "endDate": data["end_date"],
            "dates": data["dates"],
            "source": data["source"],
            "datePrecision": data["date_precision"],
            "holidayName": data["holiday_name"],
            "holidayInferred": data["holiday_inferred"],
            "exactOfficialHolidayCalendar": data["exact_official_holiday_calendar"],
            "weatherForecastSupported": data["weather_forecast_supported"],
            "reason": data["reason"],
            "weatherStatus": data["weather_status"],
            "forecastAvailableFrom": data["forecast_available_from"],
            "weatherNextAction": data["weather_next_action"],
        }


@dataclass(frozen=True)
class _ParsedDateToken:
    year: Optional[int]
    month: Optional[int]
    day: Optional[int]
    start: int
    end: int

    @property
    def is_valid(self) -> bool:
        return self.month is not None and self.day is not None and 1 <= self.month <= 12 and 1 <= self.day <= 31


class TripDateResolver:
    HOLIDAY_RE = re.compile(r"(国庆节?|十一黄金周|十一|国庆假期|黄金周)")
    _CHINESE_NUMBER_CHARS = "〇零一二两三四五六七八九十"
    _MONTH_DAY_NUMBER_PATTERN = rf"(?:\d{{1,3}}|[{_CHINESE_NUMBER_CHARS}]{{1,3}})"
    _DATE_DAY_TERMINATOR_PATTERN = rf"(?:\s*(?:日|号)|(?=$|[^\d{_CHINESE_NUMBER_CHARS}]))"
    _DATE_TOKEN_RE = re.compile(
        rf"(?:(?P<year>20\d{{2}})\s*年\s*)?"
        rf"(?P<month>{_MONTH_DAY_NUMBER_PATTERN})\s*月\s*"
        rf"(?P<day>{_MONTH_DAY_NUMBER_PATTERN}){_DATE_DAY_TERMINATOR_PATTERN}"
    )
    _DATE_LIST_SEPARATOR_RE = re.compile(r"\s*(?:以及|还有|，|,|、|和|及|与)\s*")
    _DATE_RANGE_SEPARATOR_RE = re.compile(r"\s*(?:到|至|-|－|—|–|~|～)\s*")
    _RANGE_END_RE = re.compile(
        rf"\s*(?:到|至|-|－|—|–|~|～)\s*"
        rf"(?:(?P<year>20\d{{2}})\s*年\s*)?"
        rf"(?:(?P<month>{_MONTH_DAY_NUMBER_PATTERN})\s*月\s*)?"
        rf"(?P<day>{_MONTH_DAY_NUMBER_PATTERN}){_DATE_DAY_TERMINATOR_PATTERN}"
    )

    def __init__(self, clock: Optional[Clock] = None, forecast_window_days: int = 4):
        self.clock = clock or date.today
        self.forecast_window_days = max(1, int(forecast_window_days))

    def resolve(
        self,
        latest_message: str = "",
        request_context: Optional[dict[str, Any]] = None,
        source: str = "latestUserMessage",
    ) -> ResolvedTripDates:
        context = request_context or {}
        candidates = [
            (source, latest_message),
            ("context.travelDateRange", context.get("travelDateRange")),
            ("context.dateRange", context.get("dateRange")),
            ("context.timelineContext", context.get("timelineContext")),
            ("context.understoodRequirements", context.get("understoodRequirements")),
        ]
        current_duration_without_dates = False
        for index, (candidate_source, value) in enumerate(candidates):
            resolved = self._resolve_value(value, candidate_source)
            if resolved.status == "resolved":
                if index > 0 and current_duration_without_dates:
                    # A current duration edit is a new date contract.  Reusing
                    # an older timeline merely because it happens to contain
                    # dates would silently discard or invent planning days.
                    return self._unresolved(source, "duration_without_dates")
                return resolved
            if resolved.status == "unresolved":
                # A higher-priority source containing an explicit but invalid
                # date contract must never be replaced by an older timeline or
                # a broad holiday inference.
                return resolved
            if index == 0:
                current_duration_without_dates = self._extract_explicit_duration(str(latest_message or "")) is not None
        return ResolvedTripDates(
            status="unknown",
            start_date=None,
            end_date=None,
            dates=[],
            source=source,
            date_precision="unknown",
            reason="no_supported_trip_date_expression",
            weather_forecast_supported=None,
        )

    def _resolve_value(self, value: Any, source: str) -> ResolvedTripDates:
        if value is None:
            return self._unknown(source)
        if isinstance(value, dict):
            existing = self._from_resolved_like(value, source)
            if existing.status in {"resolved", "unresolved"}:
                return existing
            start = value.get("start") or value.get("startDate") or value.get("date") or value.get("travelDate")
            end = value.get("end") or value.get("endDate")
            if start:
                return self._from_start_end(str(start), str(end or start), source, "date_range" if end else "exact_date")
            fields = value.get("fields") if isinstance(value.get("fields"), dict) else None
            if fields:
                return self._resolve_value(fields.get("travelDate") or fields.get("date"), source)
            text_parts = [value.get("summary"), value.get("latestUserMessage"), value.get("currentUserMessage")]
            text = " ".join(str(item) for item in text_parts if item)
            if text:
                return self._resolve_text(text, source)
            plan = value.get("itineraryPlan") if isinstance(value.get("itineraryPlan"), dict) else value
            dates = [
                str(day.get("date"))
                for day in plan.get("days", [])
                if isinstance(day, dict) and day.get("date")
            ] if isinstance(plan, dict) else []
            if dates:
                return self._from_start_end(dates[0], dates[-1], source, "date_range" if len(dates) > 1 else "exact_date")
            return self._unknown(source)
        return self._resolve_text(str(value), source)

    def _resolve_text(self, text: str, source: str) -> ResolvedTripDates:
        text = text.strip()
        if not text or text in {"待确认", "未知"}:
            return self._unknown(source)
        ranged = self._parse_chinese_range(text, source)
        if ranged.status == "resolved":
            return self._validate_explicit_duration(text, ranged, source)
        if ranged.status == "unresolved":
            return ranged
        listed = self._parse_explicit_date_list(text, source)
        if listed.status == "resolved":
            return self._validate_explicit_duration(text, listed, source)
        if listed.status == "unresolved":
            return listed
        exact = self._parse_exact_date(text, source)
        if exact.status == "resolved":
            return self._validate_explicit_duration(text, exact, source)
        if exact.status == "unresolved":
            return exact
        if self._contains_explicit_date_expression(text):
            return self._unresolved(source, "unparseable_explicit_date")
        relative_match = re.search(r"(?<!大)(今天|明天|后天)", text)
        if relative_match:
            offset_days = {"今天": 0, "明天": 1, "后天": 2}[relative_match.group(1)]
            resolved = self.clock() + timedelta(days=offset_days)
            return self._from_start_end(
                resolved.isoformat(),
                resolved.isoformat(),
                source,
                "relative_date",
            )
        if re.search(
            r"(?:国庆(?:节|假期)?|十一黄金周|黄金周)前\s*[一二两三四五六七1-7]\s*天",
            text,
        ):
            # Bare `国庆前两天` can mean either the two days before the
            # holiday or the first two days of it.  Do not silently choose.
            return self._unresolved(source, "ambiguous_national_day_relative_duration")
        holiday_match = self.HOLIDAY_RE.search(text)
        if holiday_match:
            year = self._extract_year(text)
            if year is None:
                today = self.clock()
                year = today.year if today <= date(today.year, 10, 7) else today.year + 1
            duration = self._national_day_leading_duration(text)
            if duration is not None:
                return self._holiday_national_day(year, source, duration_days=duration)
            explicit_duration = self._extract_explicit_duration(text)
            if explicit_duration is not None and explicit_duration != 7:
                return self._unresolved(source, "holiday_dates_unspecified")
            return self._holiday_national_day(year, source)
        return self._unknown(source)

    def _from_resolved_like(self, value: dict[str, Any], source: str) -> ResolvedTripDates:
        start = value.get("startDate") or value.get("start_date")
        end = value.get("endDate") or value.get("end_date") or start
        persisted_status = str(value.get("status") or "").strip().lower()
        if not start and persisted_status in {"unresolved", "unsupported"}:
            return self._unresolved(
                str(value.get("source") or source),
                str(value.get("reason") or "unparseable_explicit_date"),
            )
        if not start:
            return self._unknown(source)
        dates = value.get("dates") if isinstance(value.get("dates"), list) else self._date_list(str(start), str(end))
        supported = self._weather_supported(str(start))
        weather_status, forecast_available_from, next_action = self._weather_availability_fields(str(start), supported)
        return ResolvedTripDates(
            status=str(value.get("status") or "resolved"),
            start_date=str(start),
            end_date=str(end),
            dates=[str(item) for item in dates],
            source=str(value.get("source") or source),
            date_precision=str(value.get("datePrecision") or value.get("date_precision") or "date_range"),
            holiday_name=value.get("holidayName") or value.get("holiday_name"),
            holiday_inferred=bool(value.get("holidayInferred") or value.get("holiday_inferred")),
            exact_official_holiday_calendar=bool(value.get("exactOfficialHolidayCalendar") or value.get("exact_official_holiday_calendar")),
            weather_forecast_supported=supported,
            reason=None if supported else "date_outside_supported_forecast_window",
            weather_status=weather_status,
            forecast_available_from=forecast_available_from,
            weather_next_action=next_action,
        )

    def _parse_chinese_range(self, text: str, source: str) -> ResolvedTripDates:
        tokens = self._extract_date_tokens(text)
        range_groups: list[tuple[date, date]] = []
        consumed_token_indexes: set[int] = set()
        for index, start_token in enumerate(tokens):
            if index in consumed_token_indexes:
                continue
            if index + 1 < len(tokens):
                end_token = tokens[index + 1]
                gap = text[start_token.end : end_token.start]
                if self._DATE_RANGE_SEPARATOR_RE.fullmatch(gap):
                    calendar_dates = self._materialize_date_tokens([start_token, end_token])
                    if calendar_dates is None:
                        return self._unresolved(source, "invalid_calendar_date")
                    range_groups.append((calendar_dates[0], calendar_dates[1]))
                    consumed_token_indexes.update({index, index + 1})
                    continue

            shorthand_match = self._RANGE_END_RE.match(text[start_token.end :])
            if shorthand_match is None:
                continue
            end_month = (
                self._parse_month_day_number(shorthand_match.group("month"))
                if shorthand_match.group("month")
                else start_token.month
            )
            shorthand_end = _ParsedDateToken(
                year=(int(shorthand_match.group("year")) if shorthand_match.group("year") else None),
                month=end_month,
                day=self._parse_month_day_number(shorthand_match.group("day")),
                start=start_token.end + shorthand_match.start(),
                end=start_token.end + shorthand_match.end(),
            )
            calendar_dates = self._materialize_date_tokens([start_token, shorthand_end])
            if calendar_dates is None:
                return self._unresolved(source, "invalid_calendar_date")
            range_groups.append((calendar_dates[0], calendar_dates[1]))
            consumed_token_indexes.add(index)

        if not range_groups:
            return self._unknown(source)
        if len(consumed_token_indexes) != len(tokens):
            return self._unresolved(source, "ambiguous_multiple_explicit_date_groups")
        unique_ranges = {(start.isoformat(), end.isoformat()) for start, end in range_groups}
        if len(unique_ranges) != 1:
            return self._unresolved(source, "ambiguous_multiple_explicit_date_groups")
        start_date, end_date = range_groups[0]
        return self._from_start_end(
            start_date.isoformat(),
            end_date.isoformat(),
            source,
            "date_range",
        )

    def _parse_explicit_date_list(self, text: str, source: str) -> ResolvedTripDates:
        """Resolve an explicitly repeated, contiguous calendar-date list.

        A comma is not generally a range operator.  Requiring the month on
        both sides keeps expressions such as ``10月1日，2人`` out of this path,
        while accepting the reported ``10月1日，10月2日两天`` wording.  The
        whole connected list is consumed so a third date can never be silently
        discarded.  A non-contiguous list fails closed instead of inventing the
        days between independent visits.
        """

        tokens = self._extract_date_tokens(text)
        if len(tokens) < 2:
            return self._unknown(source)
        token_groups: list[list[_ParsedDateToken]] = [[tokens[0]]]
        for previous, current in zip(tokens, tokens[1:]):
            if self._DATE_LIST_SEPARATOR_RE.fullmatch(text[previous.end : current.start]):
                token_groups[-1].append(current)
            else:
                token_groups.append([current])
        if all(len(group) == 1 for group in token_groups):
            return self._unknown(source)
        if any(len(group) < 2 for group in token_groups):
            return self._unresolved(source, "ambiguous_multiple_explicit_date_groups")

        materialized_groups: list[list[date]] = []
        for token_group in token_groups:
            explicit_dates = self._materialize_date_tokens(token_group)
            if explicit_dates is None:
                return self._unresolved(source, "invalid_calendar_date")
            if any((current - previous).days != 1 for previous, current in zip(explicit_dates, explicit_dates[1:])):
                return self._unresolved(source, "non_contiguous_explicit_date_list")
            materialized_groups.append(explicit_dates)
        unique_groups = {
            tuple(item.isoformat() for item in explicit_dates)
            for explicit_dates in materialized_groups
        }
        if len(unique_groups) != 1:
            return self._unresolved(source, "ambiguous_multiple_explicit_date_groups")
        explicit_dates = materialized_groups[0]
        return self._from_start_end(
            explicit_dates[0].isoformat(),
            explicit_dates[-1].isoformat(),
            source,
            "date_range",
        )

    def _parse_exact_date(self, text: str, source: str) -> ResolvedTripDates:
        tokens = self._extract_date_tokens(text)
        if len(tokens) == 1:
            explicit_dates = self._materialize_date_tokens(tokens)
            if explicit_dates is None:
                return self._unresolved(source, "invalid_calendar_date")
            exact = explicit_dates[0]
            return self._from_start_end(
                exact.isoformat(),
                exact.isoformat(),
                source,
                "exact_date",
            )
        if len(tokens) > 1:
            explicit_dates = self._materialize_date_tokens(tokens)
            if explicit_dates is None:
                return self._unresolved(source, "invalid_calendar_date")
            unique_dates = {item.isoformat() for item in explicit_dates}
            if len(unique_dates) == 1:
                exact = explicit_dates[0]
                return self._from_start_end(
                    exact.isoformat(),
                    exact.isoformat(),
                    source,
                    "exact_date",
                )
            return self._unresolved(source, "ambiguous_multiple_explicit_date_groups")
        match = re.search(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})(?:\s*[-/.月]\s*(\d{1,2})\s*日?)", text)
        if match:
            return self._from_parts(
                int(match.group(1)), int(match.group(2)), int(match.group(3)), None, None, None, source, "exact_date"
            )
        return self._unknown(source)

    def _extract_date_tokens(self, text: str) -> list[_ParsedDateToken]:
        tokens: list[_ParsedDateToken] = []
        for match in self._DATE_TOKEN_RE.finditer(text):
            tokens.append(
                _ParsedDateToken(
                    year=int(match.group("year")) if match.group("year") else None,
                    month=self._parse_month_day_number(match.group("month")),
                    day=self._parse_month_day_number(match.group("day")),
                    start=match.start(),
                    end=match.end(),
                )
            )
        return tokens

    @classmethod
    def _parse_month_day_number(cls, raw: Optional[str]) -> Optional[int]:
        token = str(raw or "").strip().replace("〇", "零").replace("两", "二")
        if not token:
            return None
        if token.isdigit():
            return int(token)
        digit_values = {
            "零": 0,
            "一": 1,
            "二": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
        }
        if token in digit_values:
            return digit_values[token]
        if token == "十":
            return 10
        if token.count("十") != 1:
            return None
        tens_text, ones_text = token.split("十", 1)
        if len(tens_text) > 1 or len(ones_text) > 1:
            return None
        tens = 1 if not tens_text else digit_values.get(tens_text)
        ones = 0 if not ones_text else digit_values.get(ones_text)
        if tens is None or ones is None or tens == 0:
            return None
        return tens * 10 + ones

    def _materialize_date_tokens(
        self,
        tokens: list[_ParsedDateToken],
    ) -> Optional[list[date]]:
        if not tokens or any(not token.is_valid for token in tokens):
            return None
        first_explicit_year = next(
            (token.year for token in tokens if token.year is not None),
            None,
        )
        resolved_dates: list[date] = []
        for index, token in enumerate(tokens):
            if token.year is not None:
                year = token.year
            elif index == 0:
                year = first_explicit_year or self._infer_year_for_month_day(
                    int(token.month),
                    int(token.day),
                )
            else:
                year = resolved_dates[-1].year
            try:
                candidate = date(year, int(token.month), int(token.day))
                if index > 0 and token.year is None and candidate < resolved_dates[-1]:
                    candidate = date(year + 1, int(token.month), int(token.day))
            except (TypeError, ValueError):
                return None
            resolved_dates.append(candidate)
        return resolved_dates

    def _validate_explicit_duration(
        self,
        text: str,
        resolved: ResolvedTripDates,
        source: str,
    ) -> ResolvedTripDates:
        duration = self._extract_explicit_duration(text)
        if duration is None or duration == len(resolved.dates):
            return resolved
        return self._unresolved(source, "date_duration_conflict")

    def _extract_explicit_duration(self, text: str) -> Optional[int]:
        normalized_text = str(text or "")
        date_spans = [(token.start, token.end) for token in self._extract_date_tokens(normalized_text)]
        number_pattern = self._MONTH_DAY_NUMBER_PATTERN
        patterns = (
            re.compile(
                rf"(?P<count>{number_pattern})\s*"
                rf"(?:天|日(?:游|行程|旅行|游玩)|天游)"
            ),
            re.compile(
                rf"(?:玩|游玩|旅行|出行|行程|安排)\s*"
                rf"(?P<count>{number_pattern})\s*日(?!期)"
            ),
        )
        durations: list[int] = []
        for pattern in patterns:
            for match in pattern.finditer(normalized_text):
                # “第一天上午”“第 2 天晚上” identify an itinerary day;
                # they do not change the trip duration.  Treating the ordinal
                # as “一/两天” would incorrectly invalidate ordinary timeline
                # edits and could stop before the edit Controller runs.
                if re.search(r"第\s*$", normalized_text[: match.start()]):
                    continue
                if any(
                    match.start() < date_end and match.end() > date_start
                    for date_start, date_end in date_spans
                ):
                    continue
                value = self._parse_month_day_number(match.group("count"))
                if value is not None and 1 <= value <= 30:
                    durations.append(value)
        unique = set(durations)
        if not unique:
            return None
        if len(unique) != 1:
            return -1
        return durations[0]

    def _contains_explicit_date_expression(self, text: str) -> bool:
        number_pattern = self._MONTH_DAY_NUMBER_PATTERN
        return bool(
            re.search(
                rf"{number_pattern}\s*月\s*(?:初\s*)?"
                rf"{number_pattern}\s*(?:日|号)?",
                str(text or ""),
            )
        )

    def _infer_year_for_month_day(self, month: int, day: int) -> int:
        today = self.clock()
        try:
            candidate = date(today.year, month, day)
        except ValueError:
            return today.year
        return today.year if candidate >= today else today.year + 1

    def _from_parts(
        self,
        start_year: int,
        start_month: int,
        start_day: int,
        end_year: Optional[int],
        end_month: Optional[int],
        end_day: Optional[int],
        source: str,
        precision: str,
    ) -> ResolvedTripDates:
        try:
            start = date(start_year, start_month, start_day)
            end = date(end_year or start_year, end_month or start_month, end_day or start_day)
        except ValueError:
            return self._unresolved(source, "invalid_calendar_date")
        return self._from_start_end(start.isoformat(), end.isoformat(), source, precision)

    def _from_start_end(self, start_text: str, end_text: str, source: str, precision: str) -> ResolvedTripDates:
        try:
            start = datetime.strptime(start_text[:10], "%Y-%m-%d").date()
            end = datetime.strptime(end_text[:10], "%Y-%m-%d").date()
        except ValueError:
            return self._unresolved(source, "invalid_calendar_date")
        if end < start:
            start, end = end, start
        supported = self._weather_supported(start.isoformat())
        weather_status, forecast_available_from, next_action = self._weather_availability_fields(
            start.isoformat(), supported
        )
        return ResolvedTripDates(
            status="resolved",
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            dates=self._date_list(start.isoformat(), end.isoformat()),
            source=source,
            date_precision=precision,
            weather_forecast_supported=supported,
            reason=None if supported else "date_outside_supported_forecast_window",
            weather_status=weather_status,
            forecast_available_from=forecast_available_from,
            weather_next_action=next_action,
        )

    def _holiday_national_day(
        self,
        year: int,
        source: str,
        *,
        duration_days: Optional[int] = None,
    ) -> ResolvedTripDates:
        start = date(year, 10, 1)
        bounded_duration = min(7, max(1, int(duration_days or 7)))
        end = start + timedelta(days=bounded_duration - 1)
        supported = self._weather_supported(start.isoformat())
        weather_status, forecast_available_from, next_action = self._weather_availability_fields(
            start.isoformat(), supported
        )
        return ResolvedTripDates(
            status="resolved",
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            dates=self._date_list(start.isoformat(), end.isoformat()),
            source=source,
            date_precision=("holiday_duration" if duration_days is not None else "holiday_inferred"),
            holiday_name="国庆节",
            holiday_inferred=True,
            exact_official_holiday_calendar=False,
            weather_forecast_supported=supported,
            reason=None if supported else "date_outside_supported_forecast_window",
            weather_status=weather_status,
            forecast_available_from=forecast_available_from,
            weather_next_action=next_action,
        )

    @staticmethod
    def _national_day_leading_duration(text: str) -> Optional[int]:
        match = re.search(
            r"(?:国庆(?:节|假期)?|十一黄金周|黄金周)(?:(?:的\s*前)|头)\s*([一二两三四五六七1-7])\s*天",
            str(text or ""),
        )
        if not match:
            return None
        value = match.group(1)
        return int(value) if value.isdigit() else {
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
        }.get(value)

    def weather_availability_contract(self, resolved: ResolvedTripDates | dict[str, Any]) -> dict[str, Any]:
        if isinstance(resolved, ResolvedTripDates):
            data = resolved.to_camel_dict()
        else:
            data = dict(resolved or {})
        start = data.get("startDate") or data.get("start_date")
        status = str(data.get("weatherStatus") or data.get("weather_status") or "")
        supported = data.get("weatherForecastSupported")
        if not status:
            if not start:
                status = "not_requested"
            else:
                supported = self._weather_supported(str(start)) if supported is None else bool(supported)
                status, _available, _action = self._weather_availability_fields(str(start), bool(supported))
        forecast_available_from = data.get("forecastAvailableFrom") or data.get("forecast_available_from")
        next_action = data.get("weatherNextAction") or data.get("weather_next_action")
        if start and not forecast_available_from:
            _status, forecast_available_from, calculated_action = self._weather_availability_fields(
                str(start), bool(supported)
            )
            next_action = next_action or calculated_action
        return {
            "status": status or "not_requested",
            "tripStartDate": str(start) if start else None,
            "forecastAvailableFrom": forecast_available_from,
            "queriedAt": None,
            "nextAction": next_action or ("refresh_now" if status == "available" else "provide_trip_dates"),
            "source": None,
        }

    def _weather_availability_fields(self, start_date: str, supported: bool) -> tuple[str, Optional[str], str]:
        try:
            target = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
        except ValueError:
            return "unavailable", None, "provide_trip_dates"
        available_from = target - timedelta(days=self.forecast_window_days - 1)
        today = self.clock()
        if supported:
            return "available", available_from.isoformat(), "refresh_now"
        if target > today + timedelta(days=self.forecast_window_days - 1):
            return "outside_forecast_window", available_from.isoformat(), "enable_auto_refresh"
        return "stale", available_from.isoformat(), "refresh_now"

    def _weather_supported(self, start_date: str) -> bool:
        try:
            target = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
        except ValueError:
            return False
        today = self.clock()
        return today <= target <= today + timedelta(days=self.forecast_window_days - 1)

    def _date_list(self, start_date: str, end_date: str) -> list[str]:
        start = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
        end = datetime.strptime(end_date[:10], "%Y-%m-%d").date()
        return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]

    def _extract_year(self, text: str) -> Optional[int]:
        match = re.search(r"(20\d{2})", text)
        return int(match.group(1)) if match else None

    def _unknown(self, source: str) -> ResolvedTripDates:
        return ResolvedTripDates(
            status="unknown",
            start_date=None,
            end_date=None,
            dates=[],
            source=source,
            date_precision="unknown",
            weather_forecast_supported=None,
            reason="no_supported_trip_date_expression",
        )

    def _unresolved(self, source: str, reason: str) -> ResolvedTripDates:
        return ResolvedTripDates(
            status="unresolved",
            start_date=None,
            end_date=None,
            dates=[],
            source=source,
            date_precision="unknown",
            weather_forecast_supported=None,
            reason=reason,
        )
