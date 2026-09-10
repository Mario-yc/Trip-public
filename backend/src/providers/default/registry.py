from src.providers.base.interfaces import BaseProvider
from src.providers.base.results import CredibilityRank, ProviderKind, ProviderResult, ProviderStatus


class DefaultProvider(BaseProvider):
    def __init__(self, kind: ProviderKind, configured: bool):
        self.kind = kind
        self.name = f"default-{kind.value}-provider"
        self.configured = configured

    def health(self) -> ProviderResult:
        status = ProviderStatus.available if self.configured else ProviderStatus.degraded
        return ProviderResult(
            providerKind=self.kind,
            providerName=self.name,
            isMock=False,
            status=status,
            sourceName="Default provider configuration",
            credibilityRank=CredibilityRank.unknown,
            confidence=0.5 if self.configured else 0.0,
            data={
                "configured": self.configured,
                "message": "Provider key configured." if self.configured else "Provider key not configured; calls should surface explicit unavailable/degraded results.",
            },
        )


def build_default_registry(api_keys: dict[ProviderKind, str]) -> dict[ProviderKind, BaseProvider]:
    return {kind: DefaultProvider(kind, bool(api_keys.get(kind))) for kind in ProviderKind}
