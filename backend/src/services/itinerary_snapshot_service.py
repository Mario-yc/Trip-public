import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException

from src.api.schemas.itinerary_patches import ItineraryVersionResponse
from src.services.itinerary_service import ItineraryService


class ItinerarySnapshotService:
    VERSION_METADATA_KEYS = (
        "creativeBrief",
        "portfolioDayAnchorTargets",
        "portfolioDensityDecisionSource",
        "portfolioDensityEvidence",
        "portfolioPlanningProjection",
        "portfolioPlanningDirections",
        "portfolioGoalOccurrencePlan",
        "portfolioDailyCapacityPlan",
        "portfolioRequiredCandidateBindings",
        "portfolioTransportPreference",
        "portfolioRouteVerificationRequired",
        "portfolioRouteEvidence",
        "portfolioRouteQuality",
        "portfolioScheduleProjection",
        "portfolioPartialTimeline",
        "portfolioPendingSlots",
        "portfolioPendingSlotScheduleConstraints",
        "portfolioSelectionContext",
        "routeDecisionContract",
        "simpleOpenExecutionProfile",
        "simpleOpenExecutionRoute",
        "simpleOpenRouteStatus",
        "requiredPlanningDayNumbers",
        "explicitRestDayNumbers",
        "desiredDensityAnchorTargets",
        "dailyPlanningCoverageSource",
        "simpleOpenRouteAssignment",
        "routeInsertionProofs",
        "routeMatrixExpectedPairs",
    )

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def capture_snapshot(self, plan_id: str) -> dict:
        plan = ItineraryService(self.db).get_plan(plan_id)
        return plan.model_dump(by_alias=True)

    def capture_active_version_snapshot(self, plan_id: str, version_id: str) -> dict:
        """Overlay version-only planner metadata onto the current live itinerary."""
        row = self.db.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ? AND plan_id = ?",
            (version_id, plan_id),
        ).fetchone()
        if row is None:
            current_snapshot = self.capture_snapshot(plan_id)
            current_snapshot["activeVersionId"] = version_id
            return current_snapshot
        try:
            version_snapshot = json.loads(row["snapshot_json"] or "{}")
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=500, detail="Itinerary version snapshot is invalid") from error
        current_snapshot = self.capture_snapshot(plan_id)
        self.copy_version_metadata(version_snapshot, current_snapshot)
        current_snapshot["activeVersionId"] = version_id
        return current_snapshot

    @classmethod
    def copy_version_metadata(cls, source: dict, target: dict) -> dict:
        for key in cls.VERSION_METADATA_KEYS:
            if key in source:
                target[key] = source[key]
        return target

    def save_version(
        self,
        session_id: str,
        plan_id: str,
        source_type: str,
        snapshot: Optional[dict] = None,
        source_turn_id: Optional[str] = None,
        source_patch_id: Optional[str] = None,
        update_session: bool = True,
    ) -> ItineraryVersionResponse:
        snapshot = snapshot or self.capture_snapshot(plan_id)
        version = ItineraryVersionResponse(
            id=f"ver_{uuid4().hex[:12]}",
            version_number=self._next_version_number(session_id),
            source_type=source_type,
        )
        self.db.execute(
            """
            INSERT INTO itinerary_versions (
                id, session_id, plan_id, version_number, source_type,
                source_turn_id, source_patch_id, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.id,
                session_id,
                plan_id,
                version.version_number,
                version.source_type,
                source_turn_id,
                source_patch_id,
                json.dumps(snapshot, ensure_ascii=False, default=str),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        if update_session:
            self.db.execute(
                "UPDATE conversation_sessions SET active_version_id = ?, updated_at = ? WHERE id = ?",
                (version.id, datetime.now(timezone.utc).isoformat(), session_id),
            )
        return version

    def restore_existing_version(
        self, plan_id: str, version_id: str, reason: Optional[str] = None
    ) -> tuple[dict, ItineraryVersionResponse]:
        row = self.db.execute(
            "SELECT * FROM itinerary_versions WHERE id = ? AND plan_id = ?",
            (version_id, plan_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Itinerary version not found")

        snapshot = json.loads(row["snapshot_json"])
        self.apply_snapshot(plan_id, snapshot)
        restored_snapshot = self.capture_snapshot(plan_id)
        self.copy_version_metadata(snapshot, restored_snapshot)
        restored = self.save_version(
            row["session_id"],
            plan_id,
            "restore",
            snapshot=restored_snapshot,
            update_session=True,
        )
        return restored_snapshot, restored

    def apply_snapshot(self, plan_id: str, snapshot: dict) -> None:
        existing = self.db.execute("SELECT * FROM itinerary_plans WHERE id = ?", (plan_id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Itinerary plan not found")

        self.db.execute(
            """
            UPDATE itinerary_plans
            SET title = ?, city = ?, template_type = ?, budget_target = ?, budget_tier = ?,
                budget_estimate = ?, budget_delta_explanation = ?,
                decision_rationale = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                snapshot.get("title", existing["title"]),
                snapshot.get("city", existing["city"]),
                snapshot.get("templateType", existing["template_type"]),
                snapshot.get("budgetTarget"),
                snapshot.get("budgetTier", existing["budget_tier"]),
                snapshot.get("budgetEstimate", 0),
                snapshot.get("budgetDeltaExplanation", ""),
                snapshot.get("decisionRationale", ""),
                snapshot.get("status", existing["status"]),
                datetime.now(timezone.utc).isoformat(),
                plan_id,
            ),
        )
        self._clear_plan_children(plan_id)
        self._insert_snapshot_children(plan_id, snapshot)

    def _next_version_number(self, session_id: str) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(version_number), 0) AS max_version FROM itinerary_versions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["max_version"]) + 1

    def _clear_plan_children(self, plan_id: str) -> None:
        self.db.execute(
            "DELETE FROM traffic_crowding_signals WHERE route_option_id IN (SELECT id FROM route_options WHERE plan_id = ?)",
            (plan_id,),
        )
        self.db.execute(
            "DELETE FROM ticket_lookup_results WHERE segment_id IN (SELECT id FROM itinerary_segments WHERE plan_id = ?)",
            (plan_id,),
        )
        self.db.execute("DELETE FROM segment_visit_facts WHERE plan_id = ?", (plan_id,))
        self.db.execute("DELETE FROM route_options WHERE plan_id = ?", (plan_id,))
        self.db.execute("DELETE FROM weather_signals WHERE plan_id = ?", (plan_id,))
        self.db.execute("DELETE FROM poi_risk_alerts WHERE plan_id = ?", (plan_id,))
        self.db.execute("DELETE FROM itinerary_segments WHERE plan_id = ?", (plan_id,))
        self.db.execute("DELETE FROM itinerary_days WHERE plan_id = ?", (plan_id,))
        self.db.execute("DELETE FROM pois WHERE plan_id = ?", (plan_id,))

    def _insert_snapshot_children(self, plan_id: str, snapshot: dict) -> None:
        seen_pois = set()
        for day in snapshot.get("days", []):
            self.db.execute(
                """
                INSERT INTO itinerary_days (
                    id, plan_id, day_number, date, title, weather_summary,
                    risk_summary, total_estimated_cost
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day["id"],
                    plan_id,
                    day["dayNumber"],
                    day.get("date"),
                    day.get("title", ""),
                    day.get("weatherSummary", ""),
                    day.get("riskSummary", ""),
                    day.get("totalEstimatedCost", 0),
                ),
            )
            for index, segment in enumerate(day.get("segments", []), start=1):
                poi = segment["poi"]
                if poi["id"] not in seen_pois:
                    self._insert_poi(plan_id, poi)
                    seen_pois.add(poi["id"])
                self.db.execute(
                    """
                    INSERT INTO itinerary_segments (
                        id, plan_id, day_id, segment_order, kind, start_time,
                        end_time, poi_id, transport_mode, estimated_cost, estimate_metadata_json, semantic_metadata_json, notes,
                        weather_signal_id, traffic_crowding_signal_id,
                        ticket_lookup_result_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        segment["id"],
                        plan_id,
                        day["id"],
                        index,
                        segment.get("kind", "activity"),
                        segment["startTime"],
                        segment["endTime"],
                        poi["id"],
                        segment.get("transportMode", ""),
                        segment.get("estimatedCost", 0),
                        json.dumps(segment.get("estimateMetadata") or {}, ensure_ascii=False),
                        json.dumps(segment.get("semanticMetadata") or {}, ensure_ascii=False),
                        segment.get("notes", ""),
                        None,
                        None,
                        None,
                    ),
                )
        for route in snapshot.get("routeOptions", []):
            self._insert_route(plan_id, route)
        for signal in snapshot.get("weatherSignals", []):
            self._insert_weather(plan_id, signal)
        for alert in snapshot.get("poiRiskAlerts", []):
            self._insert_poi_risk_alert(plan_id, alert)
        for signal in snapshot.get("trafficCrowdingSignals", []):
            self._insert_traffic(signal)
        for ticket in snapshot.get("ticketLookupResults", []):
            self._insert_ticket(ticket)
        visit_facts = snapshot.get("visitFactsBySegment")
        if isinstance(visit_facts, dict):
            for fact_set in visit_facts.values():
                if isinstance(fact_set, dict):
                    self._insert_visit_fact(plan_id, fact_set)

    def _insert_visit_fact(self, plan_id: str, fact_set: dict) -> None:
        segment_id = str(fact_set.get("segmentId") or "")
        amap_poi_id = str(fact_set.get("amapPoiId") or "")
        visit_date = str(fact_set.get("visitDate") or "")
        expires_at = str(fact_set.get("expiresAt") or "")
        if not segment_id or not amap_poi_id or not visit_date or not expires_at:
            return
        try:
            parsed_expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if parsed_expiry.tzinfo is None:
                parsed_expiry = parsed_expiry.replace(tzinfo=timezone.utc)
        except ValueError:
            return
        if parsed_expiry <= datetime.now(timezone.utc):
            return
        current = self.db.execute(
            """
            SELECT p.amap_id, d.date
            FROM itinerary_segments s
            JOIN pois p ON p.id = s.poi_id
            JOIN itinerary_days d ON d.id = s.day_id
            WHERE s.id = ? AND s.plan_id = ?
            """,
            (segment_id, plan_id),
        ).fetchone()
        if (
            current is None
            or str(current["amap_id"] or "") != amap_poi_id
            or str(current["date"] or "date_pending") != visit_date
        ):
            return
        facts = fact_set.get("facts") if isinstance(fact_set.get("facts"), dict) else {}
        refs = fact_set.get("sourceRefs") if isinstance(fact_set.get("sourceRefs"), list) else []
        evidence_fingerprint = str(fact_set.get("evidenceFingerprint") or "")
        if not evidence_fingerprint:
            evidence_fingerprint = hashlib.sha256(
                json.dumps({"facts": facts, "refs": refs}, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
        identity = "\n".join((segment_id, amap_poi_id, visit_date))
        row_id = f"visitfact_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:18]}"
        self.db.execute(
            """
            INSERT INTO segment_visit_facts (
                id, plan_id, segment_id, amap_poi_id, visit_date, refresh_status,
                facts_json, source_refs_json, evidence_fingerprint, queried_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                plan_id,
                segment_id,
                amap_poi_id,
                visit_date,
                str(fact_set.get("refreshStatus") or "completed"),
                json.dumps(facts, ensure_ascii=False, sort_keys=True),
                json.dumps(refs, ensure_ascii=False, sort_keys=True),
                evidence_fingerprint,
                str(fact_set.get("queriedAt") or datetime.now(timezone.utc).isoformat()),
                expires_at,
            ),
        )

    def _insert_poi(self, plan_id: str, poi: dict) -> None:
        self.db.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude,
                photo_url, source, confidence, amap_id, type, district, address,
                parent_poi_id, source_note, source_url, photos_json,
                provider_type_code, tags_json, source_claims_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                poi["id"],
                plan_id,
                poi["name"],
                poi.get("city", ""),
                poi.get("category", ""),
                poi["latitude"],
                poi["longitude"],
                poi.get("photoUrl"),
                poi.get("source", ""),
                poi.get("confidence", 0),
                poi.get("amapId"),
                poi.get("type", ""),
                poi.get("district", ""),
                poi.get("address", ""),
                poi.get("parentPoiId"),
                poi.get("sourceNote", ""),
                poi.get("sourceUrl"),
                json.dumps(poi.get("photos", []), ensure_ascii=False),
                poi.get("providerTypeCode"),
                json.dumps(poi.get("tags", []), ensure_ascii=False),
                json.dumps(poi.get("sourceClaims", []), ensure_ascii=False),
            ),
        )

    def _insert_route(self, plan_id: str, route: dict) -> None:
        self.db.execute(
            """
            INSERT INTO route_options (
                id, plan_id, from_segment_id, to_segment_id, from_poi_id, to_poi_id,
                provider, mode, label, is_selected, sort_order, transport_mode,
                distance_meters, duration_seconds, duration_minutes, cost_amount,
                cost_currency, cost_estimate, crowding_risk, source, polyline_json,
                steps_json, provider_payload_json, error_json, queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                route["id"],
                plan_id,
                route.get("fromSegmentId"),
                route.get("toSegmentId"),
                route["fromPoiId"],
                route["toPoiId"],
                route.get("provider") or route.get("source", ""),
                route.get("mode") or route.get("transportMode", ""),
                route.get("label", ""),
                1 if route.get("isSelected") else 0,
                route.get("sortOrder", 0),
                route.get("transportMode") or route.get("mode", ""),
                route.get("distanceMeters", 0),
                route.get("durationSeconds", int(route.get("durationMinutes", 0) or 0) * 60),
                route.get("durationMinutes") or max(1, round(int(route.get("durationSeconds", 0) or 0) / 60)),
                route.get("costAmount", route.get("costEstimate", 0)),
                route.get("costCurrency", "CNY"),
                route.get("costEstimate", route.get("costAmount", 0)),
                route.get("crowdingRisk", ""),
                route.get("source", ""),
                json.dumps(route.get("polyline", []), ensure_ascii=False),
                json.dumps(route.get("steps", []), ensure_ascii=False),
                json.dumps(route.get("providerPayload", {}), ensure_ascii=False),
                json.dumps(route.get("error"), ensure_ascii=False) if route.get("error") else None,
                route.get("queriedAt", datetime.now(timezone.utc).isoformat()),
            ),
        )

    def _insert_weather(self, plan_id: str, signal: dict) -> None:
        self.db.execute(
            """
            INSERT INTO weather_signals (
                id, plan_id, city, date, hourly_forecast, daily_summary,
                risk_level, purpose_impact_reason, source, data_status,
                confidence, failure_reason, source_url, user_visible_caveat,
                queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal["id"],
                plan_id,
                signal.get("city", ""),
                signal.get("date", ""),
                json.dumps(signal.get("hourlyForecast", []), ensure_ascii=False),
                signal.get("dailySummary", ""),
                signal.get("riskLevel", ""),
                signal.get("purposeImpactReason", ""),
                signal.get("source", ""),
                signal.get("dataStatus", "unavailable"),
                signal.get("confidence", 0),
                signal.get("failureReason"),
                signal.get("sourceUrl"),
                signal.get("userVisibleCaveat", ""),
                signal.get("queriedAt", datetime.now(timezone.utc).isoformat()),
            ),
        )

    def _insert_poi_risk_alert(self, plan_id: str, alert: dict) -> None:
        self.db.execute(
            """
            INSERT INTO poi_risk_alerts (
                id, plan_id, segment_id, poi_name, status, summary,
                source_name, source_url, sources_json, confidence,
                failure_reason, user_visible_caveat, queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert["id"],
                plan_id,
                alert["segmentId"],
                alert.get("poiName", ""),
                alert.get("status", "unavailable"),
                alert.get("summary", ""),
                alert.get("sourceName", "搜索结果"),
                alert.get("sourceUrl"),
                json.dumps(alert.get("sources", []), ensure_ascii=False),
                alert.get("confidence", 0),
                alert.get("failureReason"),
                alert.get("userVisibleCaveat", ""),
                alert.get("queriedAt", datetime.now(timezone.utc).isoformat()),
            ),
        )

    def _insert_traffic(self, signal: dict) -> None:
        self.db.execute(
            """
            INSERT INTO traffic_crowding_signals (
                id, route_option_id, real_data_available, crowding_level,
                estimated_reason, recommended_departure_adjustment, source, queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal["id"],
                signal["routeOptionId"],
                1 if signal.get("realDataAvailable") else 0,
                signal.get("crowdingLevel", ""),
                signal.get("estimatedReason", ""),
                signal.get("recommendedDepartureAdjustment", ""),
                signal.get("source", ""),
                signal.get("queriedAt", datetime.now(timezone.utc).isoformat()),
            ),
        )

    def _insert_ticket(self, ticket: dict) -> None:
        self.db.execute(
            """
            INSERT INTO ticket_lookup_results (
                id, segment_id, ticket_type, status, price_estimate, booking_url,
                source_name, source_url, credibility_rank, queried_at, caveat,
                provider_name, fallback_used, provider_failure_reason, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket["id"],
                ticket["segmentId"],
                ticket.get("ticketType", ""),
                ticket.get("status", ""),
                ticket.get("priceEstimate", 0),
                ticket.get("bookingUrl", ""),
                ticket.get("sourceName", ""),
                ticket.get("sourceUrl", ""),
                ticket.get("credibilityRank", ""),
                ticket.get("queriedAt", datetime.now(timezone.utc).isoformat()),
                ticket.get("caveat", ""),
                ticket.get("providerName", ""),
                1 if ticket.get("fallbackUsed") else 0,
                ticket.get("providerFailureReason"),
                ticket.get("confidence", 0),
            ),
        )
