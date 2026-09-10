import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from src.api.schemas.preferences import PreferenceMemoryResponse, PreferenceSummaryCardResponse
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.models.preference_profile import PreferenceProfile
from src.models.preference_summary_card import PreferenceSummaryCard


class PreferenceService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def extract_from_text(self, text: str) -> PreferenceSummaryCardResponse:
        profile = PreferenceProfile(
            id=f"pref_{uuid4().hex[:12]}",
            user_id=get_settings().default_user_id,
            budget_range=self._extract_budget(text),
            pace_preference=self._extract_pace(text),
            transport_preferences=self._extract_transport_preferences(text),
            party_size=self._extract_party_size(text),
            traveler_types=self._extract_traveler_types(text),
        )
        card = PreferenceSummaryCard(
            id=f"card_{uuid4().hex[:12]}",
            profile_id=profile.id,
            party_size=profile.party_size,
            traveler_types=profile.traveler_types,
            budget_range=profile.budget_range,
            pace_preference=profile.pace_preference,
            items=self._extract_items(text),
            summary_text=self._summarize_preferences(text, profile),
        )
        self._insert_profile(profile)
        self._insert_card(card)
        self.db.commit()
        return self._to_response(card)

    def get_memory(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        commit: bool = True,
    ) -> PreferenceMemoryResponse:
        user_id = user_id or get_settings().default_user_id
        if session_id:
            row = self.db.execute("SELECT * FROM session_preference_memories WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                global_memory = self.get_memory(user_id=user_id, commit=False)
                now = datetime.now(timezone.utc).isoformat()
                self.db.execute(
                    """
                    INSERT INTO session_preference_memories (
                        session_id, user_id, memory_text, structured_facts_json, compiled_rules_json,
                        pending_confirmations_json, auto_update_enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        user_id,
                        global_memory.memory_text,
                        json.dumps(global_memory.structured_memory, ensure_ascii=False),
                        json.dumps(global_memory.compiled_rules, ensure_ascii=False),
                        json.dumps(global_memory.pending_confirmations, ensure_ascii=False),
                        1 if global_memory.auto_update_enabled else 0,
                        now,
                        now,
                    ),
                )
                if commit:
                    self.db.commit()
                row = self.db.execute("SELECT * FROM session_preference_memories WHERE session_id = ?", (session_id,)).fetchone()
            return self._memory_response(row)

        row = self.db.execute("SELECT * FROM travel_preference_memories WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                """
                INSERT INTO travel_preference_memories (
                    user_id, memory_text, structured_facts_json, compiled_rules_json,
                    pending_confirmations_json, auto_update_enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    DEFAULT_MEMORY_TEXT,
                    json.dumps(self._structured_memory_from_text(DEFAULT_MEMORY_TEXT), ensure_ascii=False),
                    json.dumps(self.compile_memory_rules([], []), ensure_ascii=False),
                    "[]",
                    1,
                    now,
                    now,
                ),
            )
            if commit:
                self.db.commit()
            row = self.db.execute("SELECT * FROM travel_preference_memories WHERE user_id = ?", (user_id,)).fetchone()
        return self._memory_response(row)

    def update_memory(
        self,
        memory_text: Optional[str] = None,
        auto_update_enabled: Optional[bool] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        commit: bool = True,
        structured_memory: Optional[dict[str, Any]] = None,
    ) -> PreferenceMemoryResponse:
        current = self.get_memory(user_id=user_id, session_id=session_id, commit=commit)
        next_text = memory_text if memory_text is not None else current.memory_text
        structured_source = (
            structured_memory
            if structured_memory is not None
            else self._structured_memory_from_text(next_text)
            if memory_text is not None
            else current.structured_memory
        )
        next_structured = self._normalized_structured_memory(
            structured_source,
            fallback_text=next_text,
        )
        next_pending = self._pending_confirmations(next_structured.get("facts") or [])
        next_rules = self.compile_memory_rules(next_structured.get("facts") or [], next_pending)
        next_auto_update = current.auto_update_enabled if auto_update_enabled is None else auto_update_enabled
        now = datetime.now(timezone.utc).isoformat()
        if session_id:
            self.db.execute(
                """
                UPDATE session_preference_memories
                SET memory_text = ?, structured_facts_json = ?, compiled_rules_json = ?,
                    pending_confirmations_json = ?, auto_update_enabled = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    next_text,
                    json.dumps(next_structured, ensure_ascii=False),
                    json.dumps(next_rules, ensure_ascii=False),
                    json.dumps(next_pending, ensure_ascii=False),
                    1 if next_auto_update else 0,
                    now,
                    session_id,
                ),
            )
            if commit:
                self.db.commit()
            row = self.db.execute("SELECT * FROM session_preference_memories WHERE session_id = ?", (session_id,)).fetchone()
            return self._memory_response(row)

        self.db.execute(
            """
            UPDATE travel_preference_memories
            SET memory_text = ?, structured_facts_json = ?, compiled_rules_json = ?,
                pending_confirmations_json = ?, auto_update_enabled = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                next_text,
                json.dumps(next_structured, ensure_ascii=False),
                json.dumps(next_rules, ensure_ascii=False),
                json.dumps(next_pending, ensure_ascii=False),
                1 if next_auto_update else 0,
                now,
                current.user_id,
            ),
        )
        if commit:
            self.db.commit()
        row = self.db.execute("SELECT * FROM travel_preference_memories WHERE user_id = ?", (current.user_id,)).fetchone()
        return self._memory_response(row)

    def restore_default_memory(self, user_id: Optional[str] = None, session_id: Optional[str] = None) -> PreferenceMemoryResponse:
        return self.update_memory(DEFAULT_MEMORY_TEXT, auto_update_enabled=True, user_id=user_id, session_id=session_id)

    def auto_update_memory_from_turn(
        self,
        conversation_text: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        commit: bool = True,
    ) -> PreferenceMemoryResponse:
        current = self.get_memory(user_id=user_id, session_id=session_id, commit=commit)
        if not current.auto_update_enabled:
            return current
        next_text = self._merge_memory_text(current.memory_text, conversation_text)
        next_structured = self._merge_structured_memory(current.structured_memory, conversation_text)
        self._promote_global_facts_from_session(current, next_structured, conversation_text, commit=commit)
        if next_text == current.memory_text and next_structured == current.structured_memory:
            return current
        return self.update_memory(
            memory_text=next_text,
            structured_memory=next_structured,
            auto_update_enabled=current.auto_update_enabled,
            user_id=current.user_id,
            session_id=session_id or current.session_id,
            commit=commit,
        )

    def _promote_global_facts_from_session(
        self,
        current: PreferenceMemoryResponse,
        next_structured: dict[str, Any],
        conversation_text: str,
        *,
        commit: bool,
    ) -> None:
        if not current.session_id:
            return
        current_facts = [fact for fact in current.structured_memory.get("facts") or [] if isinstance(fact, dict)]
        next_facts = [fact for fact in next_structured.get("facts") or [] if isinstance(fact, dict)]
        promoted = [
            fact
            for fact in next_facts
            if fact.get("scope") == "global"
            and fact.get("status") in {"confirmed", "inferred"}
            and not self._has_equivalent_fact(current_facts, fact)
        ]
        if not promoted:
            return
        global_memory = self.get_memory(user_id=current.user_id, commit=False)
        global_structured = self._merge_structured_memory(global_memory.structured_memory, conversation_text)
        global_text = self._merge_memory_text(global_memory.memory_text, conversation_text)
        self.update_memory(
            memory_text=global_text,
            structured_memory=global_structured,
            auto_update_enabled=global_memory.auto_update_enabled,
            user_id=current.user_id,
            commit=commit,
        )

    def update_card(
        self,
        card_id: str,
        party_size: Optional[int] = None,
        traveler_types: Optional[list[str]] = None,
        budget_range: Optional[str] = None,
        pace_preference: Optional[str] = None,
        summary_text: Optional[str] = None,
        items: Optional[list[dict]] = None,
    ) -> PreferenceSummaryCardResponse:
        row = self.db.execute("SELECT * FROM preference_summary_cards WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Preference summary card not found")

        card = PreferenceSummaryCard(
            id=row["id"],
            profile_id=row["profile_id"],
            party_size=party_size if party_size is not None else row["party_size"],
            traveler_types=traveler_types if traveler_types is not None else json.loads(row["traveler_types"]),
            budget_range=budget_range if budget_range is not None else row["budget_range"],
            pace_preference=pace_preference if pace_preference is not None else row["pace_preference"],
            summary_text=summary_text if summary_text is not None else row["summary_text"],
            items=items if items is not None else json.loads(row["items"]),
            status="revised",
            removed_items=json.loads(row["removed_items"]),
        )
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """
            UPDATE preference_summary_cards
            SET party_size = ?, traveler_types = ?, budget_range = ?,
                pace_preference = ?, summary_text = ?, items = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                card.party_size,
                json.dumps(card.traveler_types, ensure_ascii=False),
                card.budget_range,
                card.pace_preference,
                card.summary_text,
                json.dumps(card.items, ensure_ascii=False),
                card.status,
                now,
                card.id,
            ),
        )
        self.db.execute(
            """
            UPDATE preference_profiles
            SET party_size = ?, traveler_types = ?, budget_range = ?,
                pace_preference = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                card.party_size,
                json.dumps(card.traveler_types, ensure_ascii=False),
                card.budget_range,
                card.pace_preference,
                now,
                card.profile_id,
            ),
        )
        self.db.commit()
        return self._to_response(card)

    def get_profile(self, profile_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM preference_profiles WHERE id = ?", (profile_id,)).fetchone()

    def _memory_response(self, row: sqlite3.Row) -> PreferenceMemoryResponse:
        keys = set(row.keys())
        structured_memory = self._normalized_structured_memory(
            self._json_value(row["structured_facts_json"]) if "structured_facts_json" in keys else {},
            fallback_text=row["memory_text"],
        )
        pending_confirmations = (
            self._json_value(row["pending_confirmations_json"])
            if "pending_confirmations_json" in keys
            else self._pending_confirmations(structured_memory.get("facts") or [])
        )
        if not isinstance(pending_confirmations, list):
            pending_confirmations = self._pending_confirmations(structured_memory.get("facts") or [])
        compiled_rules = (
            self._json_value(row["compiled_rules_json"])
            if "compiled_rules_json" in keys
            else self.compile_memory_rules(structured_memory.get("facts") or [], pending_confirmations)
        )
        if not isinstance(compiled_rules, dict) or not compiled_rules:
            compiled_rules = self.compile_memory_rules(structured_memory.get("facts") or [], pending_confirmations)
        return PreferenceMemoryResponse(
            user_id=row["user_id"],
            session_id=row["session_id"] if "session_id" in keys else None,
            memory_text=row["memory_text"],
            structured_memory=structured_memory,
            compiled_rules=compiled_rules,
            pending_confirmations=pending_confirmations,
            auto_update_enabled=bool(row["auto_update_enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            memory_diagnostics=self._memory_diagnostics(row),
        )

    def _memory_diagnostics(self, row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        settings = get_settings()
        session_id = row["session_id"] if "session_id" in keys else None
        user_id = row["user_id"]
        global_row = self.db.execute(
            "SELECT updated_at FROM travel_preference_memories WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        session_row = (
            self.db.execute(
                "SELECT updated_at FROM session_preference_memories WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session_id
            else None
        )
        summary_card_count = self.db.execute(
            """
            SELECT COUNT(*)
            FROM preference_summary_cards cards
            LEFT JOIN preference_profiles profiles ON profiles.id = cards.profile_id
            WHERE profiles.user_id = ? OR profiles.user_id IS NULL
            """,
            (user_id,),
        ).fetchone()[0]
        return {
            "dbPath": str(sqlite_path_from_url(settings.database_url)),
            "userId": user_id,
            "sessionId": session_id,
            "source": "session" if session_id else "global",
            "globalMemoryUpdatedAt": global_row["updated_at"] if global_row else None,
            "sessionMemoryUpdatedAt": session_row["updated_at"] if session_row else None,
            "summaryCardCount": int(summary_card_count or 0),
            "autoUpdateEnabled": bool(row["auto_update_enabled"]),
            "frontendLoadedFrom": "api",
        }

    def _json_value(self, raw: Any) -> Any:
        if isinstance(raw, (dict, list)):
            return raw
        if not raw:
            return {}
        try:
            return json.loads(str(raw))
        except json.JSONDecodeError:
            return {}

    def _normalized_structured_memory(self, structured_memory: Any, fallback_text: str = "") -> dict[str, Any]:
        source = structured_memory if isinstance(structured_memory, dict) else {}
        facts = source.get("facts") if isinstance(source.get("facts"), list) else []
        normalized_facts = [self._normalized_fact(item) for item in facts if isinstance(item, dict)]
        if not normalized_facts and fallback_text:
            normalized_facts = self._structured_memory_from_text(fallback_text).get("facts") or []
        classifications = source.get("autoUpdateClassifications") if isinstance(source.get("autoUpdateClassifications"), list) else []
        return {
            "version": "travel-memory-v1",
            "facts": normalized_facts,
            "autoUpdateClassifications": classifications,
        }

    def _normalized_fact(self, fact: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        scope = str(fact.get("scope") or "session")
        if scope not in {"trip", "session", "global"}:
            scope = "session"
        status = str(fact.get("status") or "confirmed")
        if status not in {"confirmed", "inferred", "needs_confirmation"}:
            status = "confirmed"
        category = str(fact.get("category") or "general").strip() or "general"
        key = str(fact.get("key") or category).strip() or category
        value = str(fact.get("value") or "").strip()
        return {
            "id": str(fact.get("id") or f"mem_{uuid4().hex[:12]}"),
            "scope": scope,
            "category": category,
            "key": key,
            "value": value,
            "status": status,
            "source": str(fact.get("source") or "user_explicit"),
            "confidence": self._safe_confidence(fact.get("confidence"), 0.9 if status == "confirmed" else 0.65),
            "evidence": str(fact.get("evidence") or value),
            "updatedAt": str(fact.get("updatedAt") or now),
        }

    def _safe_confidence(self, value: Any, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return min(1.0, max(0.0, parsed))

    def _structured_memory_from_text(self, memory_text: str) -> dict[str, Any]:
        sections = parse_memory_sections(memory_text or DEFAULT_MEMORY_TEXT)
        facts: list[dict[str, Any]] = []
        for section, bullets in sections.items():
            category = MEMORY_SECTION_CATEGORY.get(section, "general")
            for bullet in bullets:
                value = bullet[2:].strip() if bullet.startswith("- ") else bullet.strip()
                if not value or value == "暂无明确记录。":
                    continue
                status = "needs_confirmation" if section == "需要确认" else "confirmed"
                facts.append(
                    self._new_memory_fact(
                        scope="global",
                        category=category,
                        key=category,
                        value=value,
                        status=status,
                        source="manual_markdown",
                        evidence=value,
                        confidence=0.9 if status == "confirmed" else 0.55,
                    )
                )
        return {"version": "travel-memory-v1", "facts": facts, "autoUpdateClassifications": []}

    def _merge_structured_memory(self, structured_memory: dict[str, Any], conversation_text: str) -> dict[str, Any]:
        current = self._normalized_structured_memory(structured_memory)
        facts = list(current.get("facts") or [])
        additions, classifications = self._structured_fact_additions(conversation_text)
        changed = False
        for fact in additions:
            if self._has_equivalent_fact(facts, fact):
                continue
            facts.append(fact)
            changed = True
        if not changed and not classifications:
            return current
        return {
            "version": "travel-memory-v1",
            "facts": facts,
            "autoUpdateClassifications": classifications or current.get("autoUpdateClassifications", []),
        }

    def _structured_fact_additions(self, text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        preference_text = self._statement_preference_text(text)
        if not preference_text:
            return [], []
        scope = self._classify_memory_scope(preference_text)
        additions: list[dict[str, Any]] = []
        classifications: list[dict[str, Any]] = []

        def add(category: str, key: str, value: str, *, status: str = "confirmed", source: str = "user_explicit", confidence: float = 0.9) -> None:
            fact_scope = "session" if status == "needs_confirmation" else scope
            fact = self._new_memory_fact(
                scope=fact_scope,
                category=category,
                key=key,
                value=value,
                status=status,
                source=source,
                evidence=preference_text[:220],
                confidence=confidence,
            )
            additions.append(fact)
            classifications.append(
                {
                    "factId": fact["id"],
                    "classification": status if status in {"inferred", "needs_confirmation"} else fact_scope,
                    "scope": fact_scope,
                    "status": status,
                    "category": category,
                    "reason": self._classification_reason(fact_scope, status),
                }
            )

        if any(keyword in preference_text for keyword in ("轻松", "不赶", "别太满", "不想太赶")):
            add("pace", "pace.relaxed", "偏好轻松不赶路，避免单日安排过满。")
        if any(keyword in preference_text for keyword in ("少换乘", "公共交通", "地铁", "公交", "公交地铁")):
            add("transport", "transport.public_low_transfer", "倾向公共交通和少换乘。")
        if "拍照" in preference_text or "打卡" in preference_text:
            add("interest", "interest.photo", "拍照打卡优先。")
        if "博物馆" in preference_text or "历史" in preference_text:
            add("interest", "interest.culture", "关注历史文化和博物馆。")
        if "大学" in preference_text or "校园" in preference_text or "高校" in preference_text or "学院" in preference_text:
            add("interest", "interest.campus", "本次关注高校/校园参观。")
        if any(keyword in preference_text for keyword in ("夜景", "夜游", "城市夜景", "观景")):
            add("interest", "interest.night_view", "本次关注夜景/城市观景。")
        budget = re.search(r"预算\s*(\d+)", preference_text)
        if budget:
            add("budget", "budget.amount_cny", f"预算约 {budget.group(1)} 元，需后续确认口径。")
        elif any(keyword in preference_text for keyword in ("中等预算", "预算中等", "中档预算", "中等价位", "中档价位")):
            add("budget", "budget.medium", "本次预算偏好为中等预算/中档价位。")
        party = re.search(r"(\d+)\s*人", preference_text)
        if party:
            add("party", "party.size", f"本次同行人数为 {party.group(1)} 人。")
        if any(keyword in preference_text for keyword in ("老人", "孩子", "儿童", "亲子")):
            if any(marker in preference_text for marker in ("可能", "也许", "不确定", "需要确认")):
                add("party", "party.special_needs", "同行中可能有老人、儿童或亲子需求。", status="needs_confirmation", confidence=0.55)
            else:
                add("party", "party.special_needs", "同行中有老人、儿童或亲子需求，需控制步行和排队强度。")
        if "辣" in preference_text or "清淡" in preference_text or "素食" in preference_text:
            add("food", "food.preference", "餐饮口味偏好已明确，安排用餐时需要避开不合适餐厅。")
        elif any(keyword in preference_text for keyword in ("当地特色美食", "本地美食", "特色美食", "餐饮体验", "餐厅", "美食")):
            add("food", "food.local_specialty", "本次希望体验当地特色美食/餐饮体验。", status="inferred", source="rule_inferred", confidence=0.7)
        return additions, classifications

    def _new_memory_fact(
        self,
        *,
        scope: str,
        category: str,
        key: str,
        value: str,
        status: str,
        source: str,
        evidence: str,
        confidence: float,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "id": f"mem_{uuid4().hex[:12]}",
            "scope": scope,
            "category": category,
            "key": key,
            "value": value,
            "status": status,
            "source": source,
            "confidence": confidence,
            "evidence": evidence,
            "updatedAt": now,
        }

    def _classify_memory_scope(self, text: str) -> str:
        lowered = text.lower()
        if any(marker in text for marker in ("这次", "本次", "这趟", "今天", "明天", "这几天", "本趟")):
            return "trip"
        if any(marker in lowered for marker in ("以后", "长期", "每次", "一直", "我喜欢", "我偏好", "prefer")):
            return "global"
        return "session"

    def _classification_reason(self, scope: str, status: str) -> str:
        if status == "needs_confirmation":
            return "用户语句包含不确定或待确认信息，只进入待确认队列。"
        if status == "inferred":
            return "从用户目标推断出的软偏好，可用于提示但不作为硬约束。"
        if scope == "global":
            return "用户表达为长期偏好，可 seed 到新会话。"
        if scope == "trip":
            return "用户表达限定在本次旅行。"
        return "用户表达没有长期标记，默认只作用于当前会话。"

    def _has_equivalent_fact(self, facts: list[dict[str, Any]], fact: dict[str, Any]) -> bool:
        return any(
            str(item.get("scope")) == str(fact.get("scope"))
            and str(item.get("category")) == str(fact.get("category"))
            and str(item.get("key")) == str(fact.get("key"))
            and str(item.get("value")) == str(fact.get("value"))
            and str(item.get("status")) == str(fact.get("status"))
            for item in facts
        )

    def _pending_confirmations(self, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "factId": str(fact.get("id")),
                "category": str(fact.get("category")),
                "value": str(fact.get("value")),
                "evidence": str(fact.get("evidence") or ""),
            }
            for fact in facts
            if str(fact.get("status")) == "needs_confirmation"
        ]

    @staticmethod
    def compile_memory_rules(facts: list[dict[str, Any]], pending_confirmations: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        executable_facts = [fact for fact in facts if str(fact.get("status")) in {"confirmed", "inferred"}]
        values = "\n".join(str(fact.get("value") or "") for fact in executable_facts)
        relaxed = any(marker in values for marker in ("轻松", "不赶", "过满"))
        public_low_transfer = any(marker in values for marker in ("公共交通", "少换乘", "地铁", "公交"))
        photo_first = "拍照" in values or "打卡" in values
        special_needs = any(marker in values for marker in ("老人", "儿童", "亲子", "排队强度", "步行"))
        interest_themes = []
        if "校园" in values or "大学" in values or "高校" in values:
            interest_themes.append("campus")
        if any(marker in values for marker in ("夜景", "夜游", "城市观景")):
            interest_themes.append("night_view")
        if "历史" in values or "博物馆" in values:
            interest_themes.append("culture")
        if photo_first:
            interest_themes.append("photo")
        risk_priority_terms = ["天气", "拥挤", "预约"]
        if special_needs:
            risk_priority_terms.extend(["步行强度", "排队强度"])
        return {
            "version": "travel-memory-rules-v1",
            "factsApplied": [str(fact.get("id")) for fact in executable_facts],
            "needsConfirmationFactIds": [str(item.get("factId")) for item in (pending_confirmations or [])],
            "poiSelection": {
                "preferredThemes": list(dict.fromkeys(interest_themes)),
                "preferConcreteAmapCandidates": True,
                "avoidPlaceholderPoiNames": True,
                "pureMealLabelsAreNotPois": True,
            },
            "routePlanning": {
                "anchorSegmentKinds": ["visit", "activity"],
                "excludedSegmentKinds": ["meal", "rest", "note", "transport", "buffer"],
                "preferredMode": "transit" if public_low_transfer else "",
                "minimizeTransfers": public_low_transfer,
                "avoidLongWalks": relaxed or special_needs,
            },
            "mealHandling": {
                "pureMealLabelsAreNotPois": True,
                "mealSegmentKinds": ["meal"],
                "nearbyMealSearchRequiresSpecificCuisineOrRestaurant": True,
                "mealLabels": ["早餐", "午餐", "晚餐", "早饭", "午饭", "晚饭", "中餐", "用餐", "吃饭"],
            },
            "pace": {
                "relaxed": relaxed,
                "maxVisitSegmentsPerDay": 3 if relaxed else 5,
                "softMaxTotalSegmentsPerDay": 5 if relaxed else 6,
                "requireMealOrRestBuffer": relaxed or special_needs,
                "avoidBackToBackLongVisits": True,
            },
            "riskChecks": {
                "officialSourceFirst": True,
                "checkCrowdingWeather": photo_first or special_needs,
                "priorityTerms": list(dict.fromkeys(risk_priority_terms)),
                "travelerSensitivity": "high" if special_needs else "normal",
            },
        }

    @staticmethod
    def effective_memory_text(memory_text: Optional[str]) -> str:
        """Return only user-provided preference content, excluding the empty template."""
        if not memory_text:
            return ""
        meaningful_lines: list[str] = []
        current_section = ""
        for raw_line in memory_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("# "):
                continue
            if line.startswith("## "):
                current_section = line[3:].strip()
                continue
            if line.startswith("- "):
                item = line[2:].strip()
                if item and item != "暂无明确记录。":
                    prefix = f"{current_section}：" if current_section else ""
                    meaningful_lines.append(f"{prefix}{item}")
                continue
            if line != "暂无明确记录。":
                meaningful_lines.append(line)
        return "\n".join(meaningful_lines).strip()

    def _insert_profile(self, profile: PreferenceProfile) -> None:
        self.db.execute(
            """
            INSERT INTO preference_profiles (
                id, user_id, budget_range, pace_preference, transport_preferences,
                food_preferences, photo_preference, accessibility_notes,
                party_size, traveler_types, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile.id,
                profile.user_id,
                profile.budget_range,
                profile.pace_preference,
                json.dumps(profile.transport_preferences, ensure_ascii=False),
                json.dumps(profile.food_preferences, ensure_ascii=False),
                profile.photo_preference,
                profile.accessibility_notes,
                profile.party_size,
                json.dumps(profile.traveler_types, ensure_ascii=False),
                profile.updated_at.isoformat(),
            ),
        )

    def _insert_card(self, card: PreferenceSummaryCard) -> None:
        self.db.execute(
            """
            INSERT INTO preference_summary_cards (
                id, profile_id, party_size, traveler_types, budget_range,
                pace_preference, summary_text, items, removed_items, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card.id,
                card.profile_id,
                card.party_size,
                json.dumps(card.traveler_types, ensure_ascii=False),
                card.budget_range,
                card.pace_preference,
                card.summary_text,
                json.dumps(card.items, ensure_ascii=False),
                json.dumps(card.removed_items, ensure_ascii=False),
                card.status,
                card.updated_at.isoformat(),
            ),
        )

    def _to_response(self, card: PreferenceSummaryCard) -> PreferenceSummaryCardResponse:
        return PreferenceSummaryCardResponse(
            id=card.id,
            profile_id=card.profile_id,
            party_size=card.party_size,
            traveler_types=card.traveler_types,
            budget_range=card.budget_range,
            pace_preference=card.pace_preference,
            summary_text=card.summary_text,
            items=card.items,
            status=card.status,
        )

    def _extract_party_size(self, text: str) -> int:
        if "两个大人" in text and "老人" in text:
            return 3
        match = re.search(r"(\d+)\s*人", text)
        return int(match.group(1)) if match else 0

    def _extract_traveler_types(self, text: str) -> list[str]:
        types = []
        if "大人" in text or "成人" in text:
            types.append("adult")
        if "老人" in text:
            types.append("elder")
        if "孩子" in text or "儿童" in text:
            types.append("child")
        return types

    def _extract_budget(self, text: str) -> str:
        match = re.search(r"预算\s*(\d+)", text)
        if match:
            return f"{match.group(1)} 左右"
        return ""

    def _extract_pace(self, text: str) -> str:
        if "不想太赶" in text or "轻松" in text or "不赶路" in text:
            return "轻松不赶路"
        return "未指定"

    def _extract_transport_preferences(self, text: str) -> list[str]:
        preferences = []
        if "少换乘" in text:
            preferences.append("少换乘")
        if "公共交通" in text or "地铁" in text or "公交" in text:
            preferences.append("公共交通")
        if "自驾" in text:
            preferences.append("自驾")
        return preferences

    def _extract_items(self, text: str) -> list[dict]:
        items = []
        for label in self._extract_transport_preferences(text):
            items.append({"label": label, "sourceText": label})
        return items

    def _summarize_preferences(self, text: str, profile: PreferenceProfile) -> str:
        parts = []
        if profile.pace_preference and profile.pace_preference != "未指定":
            parts.append(f"偏好{profile.pace_preference}的行程")
        if profile.budget_range:
            parts.append(f"预算约 {profile.budget_range}")
        if profile.transport_preferences:
            parts.append(f"交通上倾向{ '、'.join(profile.transport_preferences) }")
        if "拍照" in text:
            parts.append("拍照优先")
        if "避开人流" in text or "人少" in text or "不挤" in text:
            parts.append("希望避开人流密集时间")
        if not parts:
            return ""
        return f"用户{ '，'.join(parts) }。"

    def _merge_memory_text(self, memory_text: str, conversation_text: str) -> str:
        sections = parse_memory_sections(memory_text or DEFAULT_MEMORY_TEXT)
        additions = self._memory_additions(conversation_text)
        changed = False
        for section, values in additions.items():
            existing = sections.setdefault(section, [])
            for value in values:
                bullet = f"- {value}"
                if bullet not in existing:
                    existing.append(bullet)
                    changed = True
            sections[section] = [item for item in existing if item.strip() != "- 暂无明确记录。"] or ["- 暂无明确记录。"]
        if not changed:
            return memory_text
        return render_memory_sections(sections)

    def _memory_additions(self, text: str) -> dict[str, list[str]]:
        additions: dict[str, list[str]] = {}
        preference_text = self._statement_preference_text(text)

        def add(section: str, value: str) -> None:
            additions.setdefault(section, []).append(value)

        if any(keyword in preference_text for keyword in ("轻松", "不赶", "别太满", "不想太赶")):
            add("旅行节奏", "偏好轻松不赶路，避免单日安排过满。")
        if any(keyword in preference_text for keyword in ("拍照", "打卡", "博物馆", "历史", "胡同", "大学", "高校", "校园", "学院", "夜景", "夜游", "城市夜景", "观景")):
            interests = []
            if "拍照" in preference_text or "打卡" in preference_text:
                interests.append("拍照打卡")
            if "博物馆" in preference_text or "历史" in preference_text:
                interests.append("历史文化")
            if "胡同" in preference_text:
                interests.append("城市街巷")
            if "大学" in preference_text or "校园" in preference_text or "高校" in preference_text or "学院" in preference_text:
                interests.append("高校/校园参观")
            if any(keyword in preference_text for keyword in ("夜景", "夜游", "城市夜景", "观景")):
                interests.append("夜景/城市观景")
            add("兴趣偏好", f"明确提到关注{'、'.join(interests)}。")
        if any(keyword in preference_text for keyword in ("少换乘", "公共交通", "地铁", "公交")):
            add("交通偏好", "倾向公共交通和少换乘。")
        budget = re.search(r"预算\s*(\d+)", preference_text)
        if budget:
            add("预算偏好", f"预算约 {budget.group(1)} 元，需后续确认口径。")
        elif any(keyword in preference_text for keyword in ("中等预算", "预算中等", "中档预算", "中等价位", "中档价位")):
            add("预算偏好", "本次预算偏好为中等预算/中档价位。")
        party = re.search(r"(\d+)\s*人", preference_text)
        if party:
            add("同行与特殊需求", f"本次同行人数为 {party.group(1)} 人。")
        if any(keyword in preference_text for keyword in ("老人", "孩子", "儿童", "亲子")):
            add("同行与特殊需求", "同行中可能有老人、儿童或亲子需求，需控制步行和排队强度。")
        if any(keyword in preference_text for keyword in ("当地特色美食", "本地美食", "特色美食", "餐饮体验")):
            add("餐饮偏好", "本次希望体验当地特色美食/餐饮体验。")
        elif "辣" in preference_text or "清淡" in preference_text or "素食" in preference_text or "餐厅" in preference_text or "美食" in preference_text:
            add("餐饮偏好", "餐饮偏好已被提及，具体口味或餐厅类型需继续确认。")
        return additions

    def _statement_preference_text(self, text: str) -> str:
        lines = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if any(marker in line for marker in ("?", "？", "是否", "要不要", "需不需要", "请选择", "需要确认")):
                continue
            lines.append(line)
        return "\n".join(lines)


DEFAULT_MEMORY_TEXT = """# 我的旅行偏好

## 旅行节奏
- 暂无明确记录。

## 兴趣偏好
- 暂无明确记录。

## 餐饮偏好
- 暂无明确记录。

## 交通偏好
- 暂无明确记录。

## 住宿偏好
- 暂无明确记录。

## 预算偏好
- 暂无明确记录。

## 同行与特殊需求
- 暂无明确记录。

## 对话风格
- 暂无明确记录。

## 需要确认
- 暂无明确记录。
"""


MEMORY_SECTION_ORDER = [
    "旅行节奏",
    "兴趣偏好",
    "餐饮偏好",
    "交通偏好",
    "住宿偏好",
    "预算偏好",
    "同行与特殊需求",
    "对话风格",
    "需要确认",
]

MEMORY_SECTION_CATEGORY = {
    "旅行节奏": "pace",
    "兴趣偏好": "interest",
    "餐饮偏好": "food",
    "交通偏好": "transport",
    "住宿偏好": "lodging",
    "预算偏好": "budget",
    "同行与特殊需求": "party",
    "对话风格": "conversation_style",
    "需要确认": "needs_confirmation",
}


def parse_memory_sections(memory_text: str) -> dict[str, list[str]]:
    sections = {section: [] for section in MEMORY_SECTION_ORDER}
    current_section = ""
    for raw_line in memory_text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            current_section = line[3:].strip()
            sections.setdefault(current_section, [])
            continue
        if current_section and line.startswith("- "):
            sections.setdefault(current_section, []).append(line)
    return sections


def render_memory_sections(sections: dict[str, list[str]]) -> str:
    lines = ["# 我的旅行偏好", ""]
    for section in MEMORY_SECTION_ORDER:
        items = sections.get(section) or ["- 暂无明确记录。"]
        lines.extend([f"## {section}", *items, ""])
    return "\n".join(lines).rstrip() + "\n"
