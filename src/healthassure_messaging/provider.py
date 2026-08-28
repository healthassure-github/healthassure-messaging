from __future__ import annotations

from typing import Protocol, cast

from .contracts import MessageRequest, NormalizedError, SendResult
from .enums import SendDisposition


class MessagingProvider(Protocol):
    @property
    def key(self) -> str:
        """Return the stable key used to register and select this provider."""

    def send(self, request: MessageRequest) -> SendResult:
        """Send a normalized request and return its immediate provider result."""


class ProviderRegistrationError(ValueError):
    """Raised when a provider cannot be registered under the requested key."""


class DuplicateProviderError(ProviderRegistrationError):
    """Raised when a provider key is already registered."""


class ProviderNotFoundError(LookupError):
    """Raised when an explicitly selected provider is not registered."""


class ProviderContractError(RuntimeError):
    """Raised when a provider returns a result that violates the gateway contract."""


def _validate_key(key: object) -> str:
    if not isinstance(key, str) or not key.strip():
        raise ProviderRegistrationError("provider key must be a non-empty string")
    return key


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, MessagingProvider] = {}

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def register(self, key: str, provider: MessagingProvider) -> None:
        validated_key = _validate_key(key)
        if validated_key in self._providers:
            raise DuplicateProviderError(f"provider key is already registered: {validated_key}")
        if provider.key != validated_key:
            raise ProviderRegistrationError("registration key must match the provider key")
        self._providers[validated_key] = provider

    def get(self, key: str) -> MessagingProvider:
        if not isinstance(key, str) or not key.strip():
            raise ProviderNotFoundError("provider key must be supplied")
        try:
            return self._providers[key]
        except KeyError as error:
            raise ProviderNotFoundError(f"provider is not registered: {key}") from error


class MessagingGateway:
    def __init__(self, registry: ProviderRegistry) -> None:
        if not isinstance(registry, ProviderRegistry):
            raise TypeError("registry must be a ProviderRegistry")
        self._registry = registry

    def send(self, *, provider_key: str, request: MessageRequest) -> SendResult:
        provider = self._registry.get(provider_key)
        adapter_not_found = False
        provider_result: SendResult | None = None
        try:
            provider_result = provider.send(request)
        except ProviderNotFoundError:
            adapter_not_found = True

        if adapter_not_found:
            raise ProviderContractError(
                "Registered provider violated the gateway contract"
            ) from None

        result = cast(SendResult, provider_result)
        if result.provider_key != provider_key:
            raise ProviderContractError("provider result key does not match the selected provider")
        if result.correlation_id != request.correlation_id:
            raise ProviderContractError("provider result correlation_id does not match the request")
        return result


class FakeMessagingProvider:
    """Deterministic, side-effect-free provider for focused consumer and gateway tests."""

    def __init__(
        self,
        *,
        key: str,
        disposition: SendDisposition,
        provider_message_id: str | None = None,
        provider_status: str | None = None,
        error: NormalizedError | None = None,
    ) -> None:
        self._key = _validate_key(key)
        if not isinstance(disposition, SendDisposition):
            raise TypeError("disposition must be a SendDisposition")
        self._disposition = disposition
        self._provider_message_id = provider_message_id
        self._provider_status = provider_status
        self._error = error
        self._received_requests: list[MessageRequest] = []

    @property
    def key(self) -> str:
        return self._key

    @property
    def received_requests(self) -> tuple[MessageRequest, ...]:
        return tuple(self._received_requests)

    def send(self, request: MessageRequest) -> SendResult:
        if not isinstance(request, MessageRequest):
            raise TypeError("request must be a MessageRequest")
        self._received_requests.append(request)
        return SendResult(
            provider_key=self.key,
            disposition=self._disposition,
            correlation_id=request.correlation_id,
            provider_message_id=self._provider_message_id,
            provider_status=self._provider_status,
            error=self._error,
        )
