from __future__ import annotations

from dataclasses import dataclass

from .enums import ErrorCategory, SendDisposition, TemplateComponentType
from .phone import validate_e164_number


def _require_non_empty_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_optional_text(value: object, field_name: str) -> None:
    if value is not None:
        _require_non_empty_text(value, field_name)


def _require_bool(value: object, field_name: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a boolean")


@dataclass(frozen=True, slots=True)
class NormalizedError:
    category: ErrorCategory
    safe_message: str
    retriable: bool
    unknown_outcome: bool
    provider_code: str | None = None
    http_status: int | None = None
    provider_subcode: int | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.category, ErrorCategory):
            raise TypeError("category must be an ErrorCategory")
        _require_non_empty_text(self.safe_message, "safe_message")
        _require_bool(self.retriable, "retriable")
        _require_bool(self.unknown_outcome, "unknown_outcome")
        _require_optional_text(self.provider_code, "provider_code")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("http_status must be an integer between 100 and 599")
        if self.provider_subcode is not None and (
            type(self.provider_subcode) is not int
            or not 0 <= self.provider_subcode <= 2_147_483_647
        ):
            raise ValueError("provider_subcode must be a non-negative 32-bit integer")
        if self.retry_after_seconds is not None and (
            type(self.retry_after_seconds) is not int
            or not 0 <= self.retry_after_seconds <= 86_400
        ):
            raise ValueError("retry_after_seconds must be an integer between 0 and 86400")


@dataclass(frozen=True, slots=True)
class TextMessage:
    body: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.body, "body")


@dataclass(frozen=True, slots=True)
class TemplateReference:
    name: str
    language_code: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.name, "name")
        _require_non_empty_text(self.language_code, "language_code")


@dataclass(frozen=True, slots=True)
class TextParameter:
    text: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.text, "text")


@dataclass(frozen=True, slots=True)
class TemplateComponent:
    component_type: TemplateComponentType
    parameters: tuple[TextParameter, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.component_type, TemplateComponentType):
            raise TypeError("component_type must be a TemplateComponentType")
        if not isinstance(self.parameters, tuple):
            raise TypeError("parameters must be a tuple")
        if not all(isinstance(parameter, TextParameter) for parameter in self.parameters):
            raise TypeError("parameters must contain only TextParameter values")


@dataclass(frozen=True, slots=True)
class TemplateMessage:
    template: TemplateReference
    components: tuple[TemplateComponent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.template, TemplateReference):
            raise TypeError("template must be a TemplateReference")
        if not isinstance(self.components, tuple):
            raise TypeError("components must be a tuple")
        if not all(isinstance(component, TemplateComponent) for component in self.components):
            raise TypeError("components must contain only TemplateComponent values")


Message = TextMessage | TemplateMessage


@dataclass(frozen=True, slots=True)
class MessageRequest:
    recipient: str
    message: Message
    correlation_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        validate_e164_number(self.recipient)
        if not isinstance(self.message, (TextMessage, TemplateMessage)):
            raise TypeError("message must be a TextMessage or TemplateMessage")
        _require_non_empty_text(self.correlation_id, "correlation_id")
        _require_non_empty_text(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class SendResult:
    provider_key: str
    disposition: SendDisposition
    correlation_id: str
    provider_message_id: str | None = None
    provider_status: str | None = None
    error: NormalizedError | None = None

    def __post_init__(self) -> None:
        _require_non_empty_text(self.provider_key, "provider_key")
        if not isinstance(self.disposition, SendDisposition):
            raise TypeError("disposition must be a SendDisposition")
        _require_non_empty_text(self.correlation_id, "correlation_id")
        _require_optional_text(self.provider_message_id, "provider_message_id")
        _require_optional_text(self.provider_status, "provider_status")
        if self.error is not None and not isinstance(self.error, NormalizedError):
            raise TypeError("error must be a NormalizedError or None")
