from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import NormalizedError
from .enums import IntentState, SendDisposition, TemplateComponentType


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_optional_text(value: object, field_name: str) -> None:
    if value is not None:
        _require_text(value, field_name)


@dataclass(frozen=True, slots=True)
class TemplateComponentSpec:
    component_type: TemplateComponentType
    parameter_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.component_type, TemplateComponentType):
            raise TypeError("component_type must be a TemplateComponentType")
        if self.component_type not in (
            TemplateComponentType.HEADER,
            TemplateComponentType.BODY,
        ):
            raise ValueError("template components support only header and body text parameters")
        if not isinstance(self.parameter_names, tuple):
            raise TypeError("parameter_names must be a tuple")
        if not self.parameter_names:
            raise ValueError("parameter_names must not be empty")
        for name in self.parameter_names:
            _require_text(name, "parameter name")


@dataclass(frozen=True, slots=True)
class TemplateAlias:
    key: str
    provider_key: str
    template_name: str
    language_code: str
    components: tuple[TemplateComponentSpec, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.key, "key")
        _require_text(self.provider_key, "provider_key")
        _require_text(self.template_name, "template_name")
        _require_text(self.language_code, "language_code")
        if not isinstance(self.components, tuple):
            raise TypeError("components must be a tuple")
        if not all(isinstance(component, TemplateComponentSpec) for component in self.components):
            raise TypeError("components must contain only TemplateComponentSpec values")


@dataclass(frozen=True, slots=True)
class RecipientEligibility:
    consented: bool
    suppressed: bool

    def __post_init__(self) -> None:
        if type(self.consented) is not bool:
            raise TypeError("consented must be a boolean")
        if type(self.suppressed) is not bool:
            raise TypeError("suppressed must be a boolean")


@dataclass(frozen=True, slots=True)
class DispatchResult:
    intent_id: str
    provider_key: str
    disposition: SendDisposition
    correlation_id: str
    provider_message_id: str | None = None
    provider_status: str | None = None
    error: NormalizedError | None = None
    idempotent_replay: bool = False

    def __post_init__(self) -> None:
        _require_text(self.intent_id, "intent_id")
        _require_text(self.provider_key, "provider_key")
        if not isinstance(self.disposition, SendDisposition):
            raise TypeError("disposition must be a SendDisposition")
        _require_text(self.correlation_id, "correlation_id")
        _require_optional_text(self.provider_message_id, "provider_message_id")
        _require_optional_text(self.provider_status, "provider_status")
        if self.error is not None and not isinstance(self.error, NormalizedError):
            raise TypeError("error must be a NormalizedError or None")
        if type(self.idempotent_replay) is not bool:
            raise TypeError("idempotent_replay must be a boolean")


@dataclass(frozen=True, slots=True)
class MessageIntent:
    intent_id: str
    source_flow: str
    idempotency_key: str = field(repr=False)
    actor_id: str
    provider_key: str
    correlation_id: str
    request_fingerprint: str = field(repr=False)
    serialized_request: str = field(repr=False)
    state: IntentState = IntentState.PENDING
    result: DispatchResult | None = None

    def __post_init__(self) -> None:
        _require_text(self.intent_id, "intent_id")
        _require_text(self.source_flow, "source_flow")
        _require_text(self.idempotency_key, "idempotency_key")
        _require_text(self.actor_id, "actor_id")
        _require_text(self.provider_key, "provider_key")
        _require_text(self.correlation_id, "correlation_id")
        _require_text(self.request_fingerprint, "request_fingerprint")
        _require_text(self.serialized_request, "serialized_request")
        if not isinstance(self.state, IntentState):
            raise TypeError("state must be an IntentState")
        if self.result is not None and not isinstance(self.result, DispatchResult):
            raise TypeError("result must be a DispatchResult or None")
        if self.state in (IntentState.PENDING, IntentState.DISPATCHING):
            if self.result is not None:
                raise ValueError("non-terminal intents must not contain a result")
        elif self.result is None:
            raise ValueError("terminal intents must contain a result")
        if self.result is not None:
            if self.result.intent_id != self.intent_id:
                raise ValueError("result intent_id must match the intent")
            if self.result.provider_key != self.provider_key:
                raise ValueError("result provider_key must match the intent")
            if self.result.correlation_id != self.correlation_id:
                raise ValueError("result correlation_id must match the intent")
            expected_state = {
                SendDisposition.ACCEPTED: IntentState.ACCEPTED,
                SendDisposition.REJECTED: IntentState.REJECTED,
                SendDisposition.UNKNOWN: IntentState.UNKNOWN,
            }[self.result.disposition]
            if self.state is not expected_state:
                raise ValueError("terminal intent state must match the result disposition")


@dataclass(frozen=True, slots=True)
class IntentCreationResult:
    intent: MessageIntent
    created: bool

    def __post_init__(self) -> None:
        if not isinstance(self.intent, MessageIntent):
            raise TypeError("intent must be a MessageIntent")
        if type(self.created) is not bool:
            raise TypeError("created must be a boolean")


class MessagingServiceError(RuntimeError):
    """Base class for safe service-layer failures."""


class UnknownTemplateError(MessagingServiceError, LookupError):
    """Raised when a template alias is not registered."""


class TemplateParameterError(MessagingServiceError, ValueError):
    """Raised when supplied template parameters do not match an alias."""


class MissingTemplateParameterError(TemplateParameterError):
    """Raised when one or more required template parameters are absent."""


class ExtraTemplateParameterError(TemplateParameterError):
    """Raised when a template receives unrecognized parameters."""


class DuplicateTemplateAliasError(MessagingServiceError, ValueError):
    """Raised when an in-memory catalog receives a duplicate alias key."""


class IdempotencyConflictError(MessagingServiceError, ValueError):
    """Raised when an idempotency scope is reused for a different request."""


class IntentInProgressError(MessagingServiceError):
    """Raised when an existing intent already owns the dispatch claim."""


class IntentStateError(MessagingServiceError):
    """Raised when durable intent state changes violate the service contract."""


class ServiceDependencyError(MessagingServiceError):
    """Raised when an injected policy or persistence dependency fails safely."""


class PostDispatchPersistenceError(ServiceDependencyError):
    """Provider invocation occurred but its result could not be persisted."""
