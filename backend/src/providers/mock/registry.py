from src.providers.base.interfaces import BaseProvider
from src.providers.base.results import CredibilityRank, ProviderKind, ProviderResult, ProviderStatus


class MockProvider(BaseProvider):
    def __init__(self, kind: ProviderKind):
        self.kind = kind
        self.name = f"mock-{kind.value}-provider"

    def health(self) -> ProviderResult:
        return ProviderResult(
            providerKind=self.kind,
            providerName=self.name,
            isMock=True,
            status=ProviderStatus.available,
            sourceName="Mock provider",
            credibilityRank=CredibilityRank.mock,
            confidence=1.0,
            data={"message": "Deterministic mock provider is available."},
        )


def build_mock_registry() -> dict[ProviderKind, BaseProvider]:
    return {kind: MockProvider(kind) for kind in ProviderKind}
