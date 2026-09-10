from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Optional


DEFAULT_MAX_SKILL_CONTEXT_CHARS = 1200
DEFAULT_SKILL_LIMIT = 2


@dataclass(frozen=True)
class AgentSkill:
    name: str
    title: str
    tags: tuple[str, ...]
    content: str
    applies_to: tuple[str, ...] = ()
    max_context_chars: Optional[int] = None

    def to_context_item(self, text: str) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "tags": list(self.tags),
            "appliesTo": list(self.applies_to),
            "text": text,
        }


class AgentSkillService:
    def __init__(self, skills_dir: Optional[Path] = None, max_context_chars: int = DEFAULT_MAX_SKILL_CONTEXT_CHARS):
        backend_root = Path(__file__).resolve().parents[2]
        self.skills_dir = skills_dir or backend_root / "skills"
        self.max_context_chars = max_context_chars

    def load_skills(self) -> list[AgentSkill]:
        if not self.skills_dir.exists():
            return []
        skills = []
        for path in sorted(self.skills_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                continue
            skills.append(self._parse_skill(path, text))
        return skills

    def select_skills(
        self,
        user_message: str,
        request_context: Optional[dict[str, Any]] = None,
        limit: int = DEFAULT_SKILL_LIMIT,
        max_chars: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        context = request_context or {}
        ranked = sorted(
            (
                (self._score_skill(skill, user_message, context), skill)
                for skill in self.load_skills()
            ),
            key=lambda item: (-item[0], item[1].name),
        )
        selected = [skill for score, skill in ranked if score > 0][:limit]
        if not selected:
            selected = [skill for _score, skill in ranked[:1]]
        return self._trim_selected(selected, max_chars or self.max_context_chars)

    def build_skill_context(self, selected_skills: list[dict[str, Any]], max_chars: Optional[int] = None) -> str:
        budget = max_chars or self.max_context_chars
        sections = []
        used = 0
        for skill in selected_skills:
            text = f"## {skill['title']}\n{skill['text']}".strip()
            remaining = budget - used
            if remaining <= 0:
                break
            if len(text) > remaining:
                text = text[: max(0, remaining - 3)].rstrip() + "..."
            sections.append(text)
            used += len(text) + 2
        return "\n\n".join(sections)

    def _trim_selected(self, skills: list[AgentSkill], max_chars: int) -> list[dict[str, Any]]:
        if not skills:
            return []
        per_skill_budget = max(80, max_chars // len(skills) - 32)
        selected = []
        for skill in skills:
            skill_budget = min(per_skill_budget, skill.max_context_chars) if skill.max_context_chars else per_skill_budget
            selected.append(skill.to_context_item(self._trim_text(skill.content, skill_budget)))
        return selected

    def _parse_skill(self, path: Path, text: str) -> AgentSkill:
        frontmatter, body = self._split_frontmatter(text)
        metadata = self._parse_metadata(frontmatter)
        text = body
        lines = text.splitlines()
        title = str(metadata.get("title") or path.stem.replace("_", " ").title())
        tags: tuple[str, ...] = self._metadata_list(metadata.get("tags"))
        applies_to: tuple[str, ...] = self._metadata_list(metadata.get("applies_to") or metadata.get("appliesTo"))
        max_context_chars = self._metadata_int(metadata.get("max_context_chars") or metadata.get("maxContextChars"))
        body_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("# ") and title == path.stem.replace("_", " ").title():
                title = stripped[2:].strip()
                continue
            if stripped.lower().startswith("tags:") and not tags:
                tags = tuple(
                    tag.strip().lower()
                    for tag in stripped.split(":", 1)[1].split(",")
                    if tag.strip()
                )
                continue
            if stripped.lower().startswith(("applies_to:", "appliesto:")) and not applies_to:
                applies_to = self._metadata_list(stripped.split(":", 1)[1])
                continue
            if stripped.lower().startswith(("max_context_chars:", "maxcontextchars:")) and max_context_chars is None:
                max_context_chars = self._metadata_int(stripped.split(":", 1)[1])
                continue
            body_lines.append(line)
        return AgentSkill(
            name=path.stem,
            title=title,
            tags=tags,
            content="\n".join(body_lines).strip(),
            applies_to=applies_to,
            max_context_chars=max_context_chars,
        )

    def _score_skill(self, skill: AgentSkill, user_message: str, context: dict[str, Any]) -> int:
        text = self._search_text(user_message, context)
        score = sum(2 for tag in skill.tags if tag and tag in text)
        score += self._context_boost(skill.name, user_message, context)
        if skill.applies_to:
            intent = self._context_intent(user_message, context)
            if intent in skill.applies_to:
                score += 4
            elif score > 0:
                score -= 1
        return score

    def _context_boost(self, skill_name: str, user_message: str, context: dict[str, Any]) -> int:
        text = user_message.lower()
        has_itinerary = bool(context.get("currentItinerarySnapshot") or context.get("timelineContext"))
        has_pending_pois = bool(context.get("pendingAmapPoiCandidates"))
        has_selected_poi = bool(context.get("selectedMapPoi") or context.get("selectedMapPoiId"))
        patch_intent = self._has_any(text, ["改", "调整", "移动", "删除", "添加", "加入", "时间", "标题", "放到", "patch"])
        poi_intent = self._has_any(text, ["poi", "景点", "地点", "高德", "地图", "坐标", "餐厅", "博物馆", "门票"])
        rollback_intent = self._has_any(text, ["历史", "编辑之前", "回滚", "撤回", "重新生成", "上一轮"])
        source_intent = self._has_any(text, ["来源", "官方", "票务", "预约", "开放时间", "mock", "fallback", "搜索"])

        if skill_name == "patch_before_write" and (has_itinerary or patch_intent):
            return 6
        if skill_name == "pending_poi_state_machine" and has_pending_pois:
            return 8
        if skill_name == "amap_poi_grounding" and (poi_intent or has_selected_poi):
            return 7
        if skill_name == "rollback_supersession" and rollback_intent:
            return 8
        if skill_name == "source_transparency" and source_intent:
            return 7
        if skill_name == "amap_poi_grounding" and not has_itinerary:
            return 2
        if skill_name == "patch_before_write":
            return 1
        return 0

    def _search_text(self, user_message: str, context: dict[str, Any]) -> str:
        values = [user_message]
        for key in ("selectedCity", "currentPreferenceSummary", "memoryText"):
            value = context.get(key)
            if value:
                values.append(str(value))
        if context.get("pendingAmapPoiCandidates"):
            values.append("pending poi candidate 多候选 低置信 确认")
        if context.get("selectedMapPoi") or context.get("candidateMapPois"):
            values.append("amap poi 高德 地图")
        if context.get("currentItinerarySnapshot") or context.get("timelineContext"):
            values.append("itinerary patch version 行程 修改")
        return " ".join(values).lower()

    def _trim_text(self, text: str, max_chars: int) -> str:
        normalized = re.sub(r"\n{3,}", "\n\n", text.strip())
        if len(normalized) <= max_chars:
            return normalized
        return normalized[: max(0, max_chars - 3)].rstrip() + "..."

    def _has_any(self, text: str, keywords: list[str]) -> bool:
        return any(keyword.lower() in text for keyword in keywords)

    def _context_intent(self, user_message: str, context: dict[str, Any]) -> str:
        text = user_message.lower()
        if self._has_any(text, ["历史", "编辑之前", "回滚", "撤回", "重新生成", "上一轮"]):
            return "rollback"
        if context.get("pendingAmapPoiCandidates"):
            return "pending_poi"
        if self._has_any(text, ["来源", "官方", "票务", "预约", "开放时间", "搜索"]):
            return "source"
        if context.get("selectedMapPoi") or self._has_any(text, ["poi", "景点", "地点", "高德", "地图", "坐标"]):
            return "poi"
        if context.get("currentItinerarySnapshot") or self._has_any(text, ["改", "调整", "移动", "删除", "添加", "加入", "时间", "标题", "patch"]):
            return "patch"
        return "draft"

    def _split_frontmatter(self, text: str) -> tuple[str, str]:
        if not text.startswith("---"):
            return "", text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return "", text
        return parts[1].strip(), parts[2].lstrip()

    def _parse_metadata(self, text: str) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
        return metadata

    def _metadata_list(self, value: Any) -> tuple[str, ...]:
        if not value:
            return ()
        return tuple(item.strip().lower() for item in str(value).split(",") if item.strip())

    def _metadata_int(self, value: Any) -> Optional[int]:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
