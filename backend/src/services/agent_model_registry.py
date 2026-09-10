from dataclasses import dataclass
from typing import Optional


DEFAULT_AGENT_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class AgentModelInfo:
    id: str
    label: str
    provider_model: str


AGENT_MODELS = {
    "deepseek-v4-flash": AgentModelInfo(
        id="deepseek-v4-flash",
        label="deepseek-v4-flash",
        provider_model="deepseek-v4-flash",
    ),
    "deepseek-v4-pro": AgentModelInfo(
        id="deepseek-v4-pro",
        label="deepseek-v4-pro",
        provider_model="deepseek-v4-pro",
    ),
}

LEGACY_AGENT_MODEL_ALIASES = {
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-reasoner": "deepseek-v4-pro",
}


def resolve_agent_model(value: Optional[str]) -> AgentModelInfo:
    key = str(value or "").strip()
    key = LEGACY_AGENT_MODEL_ALIASES.get(key, key)
    if key in AGENT_MODELS:
        return AGENT_MODELS[key]
    for model in AGENT_MODELS.values():
        if key == model.provider_model:
            return model
    return AGENT_MODELS[DEFAULT_AGENT_MODEL]


def normalize_provider_model_alias(value: Optional[str]) -> str:
    key = str(value or "").strip()
    if not key:
        return AGENT_MODELS[DEFAULT_AGENT_MODEL].provider_model
    canonical_key = LEGACY_AGENT_MODEL_ALIASES.get(key)
    if canonical_key:
        return AGENT_MODELS[canonical_key].provider_model
    return key


def agent_model_options() -> list[dict[str, str]]:
    return [
        {"id": model.id, "label": model.label}
        for model in AGENT_MODELS.values()
    ]
