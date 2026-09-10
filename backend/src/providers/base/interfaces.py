from abc import ABC, abstractmethod

from src.providers.base.results import ProviderKind, ProviderResult


class BaseProvider(ABC):
    kind: ProviderKind
    name: str

    @abstractmethod
    def health(self) -> ProviderResult:
        """Return provider health in the normalized provider result format."""


class LLMProvider(BaseProvider):
    kind = ProviderKind.llm


class VisionProvider(BaseProvider):
    kind = ProviderKind.vision


class MapProvider(BaseProvider):
    kind = ProviderKind.map


class TicketProvider(BaseProvider):
    kind = ProviderKind.ticket


class WeatherProvider(BaseProvider):
    kind = ProviderKind.weather


class TrafficProvider(BaseProvider):
    kind = ProviderKind.traffic


class SearchProvider(BaseProvider):
    kind = ProviderKind.search


class EmailProvider(BaseProvider):
    kind = ProviderKind.email
